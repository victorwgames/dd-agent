#!/usr/bin/python

'''
A Python Statsd implementation with some datadog special sauce.
'''

import httplib as http_client
import logging
import optparse
import random
import re
import socket
import sys
import time
import threading
from urllib import urlencode

from util import json

# create logger
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
# create console handler and set level to debug
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
# create formatter
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# add formatter to ch
ch.setFormatter(formatter)
logger.addHandler(ch)


logger = logging.getLogger('dogstatsd')


class Metric(object):
    """
    A base metric class that accepts points, slices them into time intervals
    and performs roll-ups within those intervals.
    """

    def sample(self, value, sample_rate):
        """ Add a point to the given metric. """
        raise NotImplementedError()

    def flush(self, timestamp):
        """ Flush all metrics up to the given timestamp. """
        raise NotImplementedError()


class Gauge(Metric):
    """ A metric that tracks a value at particular points in time. """

    def __init__(self, name, tags):
        self.name = name
        self.value = None
        self.tags = tags

    def sample(self, value, sample_rate):
        self.value = value

    def flush(self, timestamp):
        return [{
            'metric' : self.name,
            'points' : [(timestamp, self.value)],
            'tags' : self.tags
        }]


class Counter(Metric):
    """ A metric that tracks a counter value. """

    def __init__(self, name, tags):
        self.name = name
        self.value = 0
        self.tags = tags

    def sample(self, value, sample_rate):
        self.value += value * int(1 / sample_rate)

    def flush(self, timestamp):
        return [{
            'metric' : self.name,
            'points' : [(timestamp, self.value)],
            'tags' : self.tags
        }]


class Histogram(Metric):
    """ A metric to track the distribution of a set of values. """

    def __init__(self, name, tags):
        self.name = name
        self.max = float("-inf")
        self.min = float("inf")
        self.sum = 0
        self.count = 0
        self.sample_size = 1000
        self.samples = []
        self.percentiles = [0.75, 0.85, 0.95, 0.99]
        self.tags = tags

    def sample(self, value, sample_rate):
        count = int(1 / sample_rate)
        self.max = self.max if self.max > value else value
        self.min = self.min if self.min < value else value
        self.sum += value * count
        # Is there a cleaner way to do this?
        for i in xrange(count):
            if self.count < self.sample_size:
                self.samples.append(value)
            else:
                self.samples[random.randrange(0, self.sample_size)] = value
        self.count += count

    def flush(self, ts):
        if not self.count:
            return []

        metrics = [
            {'tags': self.tags, 'metric' : '%s.min' % self.name, 'points' : [(ts, self.min)]},
            {'tags': self.tags, 'metric' : '%s.max' % self.name, 'points' : [(ts, self.max)]},
            {'tags': self.tags, 'metric' : '%s.avg' % self.name, 'points' : [(ts, self.average())]},
            {'tags': self.tags, 'metric' : '%s.count' % self.name, 'points' : [(ts, self.count)]},
        ]

        length = len(self.samples)
        self.samples.sort()
        for p in self.percentiles:
            val = self.samples[int(round(p * length - 1))]
            name = '%s.%spercentile' % (self.name, int(p * 100))
            metrics.append({'tags':self.tags, 'metric': name, 'points': [(ts, val)]})
        return metrics

    def average(self):
        return float(self.sum) / self.count



class MetricsAggregator(object):
    """
    A metric aggregator class.
    """

    def __init__(self):
        self.metrics = {}
        self.count = 0
        self.metric_type_to_class = {
            'g': Gauge,
            'c': Counter,
            'h': Histogram,
            'ms' : Histogram
        }

    def submit(self, packet):
        self.count += 1
        # We can have colons in tags, so split once.
        name_and_metadata = packet.split(':', 1)

        if len(name_and_metadata) != 2:
            raise Exception('Unparseable packet: %s' % packet)

        name = name_and_metadata[0]
        metadata = name_and_metadata[1].split('|')

        if len(metadata) < 2:
            raise Exception('Unparseable packet: %s' % packet)

        # Get the value & type of the metric.
        value = float(metadata[0])
        type_ = metadata[1]

        # Parse the optional values - sample rate & tags.
        sample_rate = 1
        tags = None
        for m in metadata[2:]:
            # Parse the sample rate
            if m[0] == '@':
                sample_rate = float(m[1:])
                assert 0 <= sample_rate <= 1
            elif m[0] == '#':
                tags = tuple(sorted(m[1:].split(',')))

        context = (name, tags)

        if context not in self.metrics:
            metric_class = self.metric_type_to_class[type_]
            self.metrics[context] = metric_class(name, tags)
        self.metrics[context].sample(value, sample_rate)


    def flush(self, timestamp=None):
        timestamp = timestamp or time.time()
        metrics = []
        for context, metric in self.metrics.items():
            metrics += metric.flush(timestamp)
            del self.metrics[context]
        logger.info("received %s payloads since last flush" % self.count)
        self.count = 0
        return metrics



class Reporter(threading.Thread):
    """
    The reporter periodically sends the aggregated metrics to the
    server.
    """

    def __init__(self, interval, metrics_aggregator, api_host, api_key=None):
        threading.Thread.__init__(self)
        self.daemon = True
        self.interval = int(interval)
        self.finished = threading.Event()
        self.metrics_aggregator = metrics_aggregator
        self.flush_count = 0

        self.api_key = api_key
        self.api_host = api_host

        self.http_conn_cls = http_client.HTTPSConnection

        match = re.match('^(https?)://(.*)', api_host)

        if match:
            self.api_host = match.group(2)
            if match.group(1) == 'http':
                self.http_conn_cls = http_client.HTTPConnection

    def end(self):
        self.finished.set()

    def run(self):
        while True:
            if self.finished.is_set():
                break
            self.finished.wait(self.interval)
            self.flush()

    def flush(self):
        try:
            self.flush_count += 1
            metrics = self.metrics_aggregator.flush()
            count = len(metrics)
            if not count:
                logger.info("Flush #{0}: No metrics to flush.".format(self.flush_count))
                return
            logger.info("Flush #{0}: flushing {1} metrics".format(self.flush_count, count))
            self.submit(metrics)
        except:
            logger.exception("Error flushing metrics")

    def submit(self, metrics):

        # HACK - Copy and pasted from dogapi, because it's a bit of a pain to distribute python
        # dependencies with the agent.
        conn = self.http_conn_cls(self.api_host)
        body = json.dumps({"series" : metrics})
        headers = {'Content-Type':'application/json'}
        method = 'POST'

        params = {}
        if self.api_key:
            params['api_key'] = self.api_key
        url = '/api/v1/series?%s' % urlencode(params)

        start_time = time.time()
        conn.request(method, url, body, headers)

        #FIXME: add timeout handling code here

        response = conn.getresponse()
        duration = round((time.time() - start_time) * 1000.0, 4)
        logger.info("%s %s %s%s (%sms)" % (
                        response.status, method, self.api_host, url, duration))

class Server(object):
    """
    A statsd udp server.
    """

    def __init__(self, metrics_aggregator, host, port):
        self.host = host
        self.port = int(port)
        self.address = (self.host, self.port)

        self.metrics_aggregator = metrics_aggregator

        self.buffer_size = 1024
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(self.address)

    def start(self):
        """ Run the server. """
        logger.info('Starting dogstatsd server on %s' % str(self.address))
        while True:
            try:
                data = self.socket.recv(self.buffer_size)
                self.metrics_aggregator.submit(data)
            except (KeyboardInterrupt, SystemExit):
                break
            except:
                logger.exception('Error receiving datagram')


def main():
    parser = optparse.OptionParser("usage: %prog [options] api_key")
    parser.add_option("-H", '--host', dest='host', default='localhost')
    parser.add_option("-p", '--port', dest='port', default='8125')
    parser.add_option("-a", '--api-host', dest='api_host', default='https://app.datadoghq.com')
    parser.add_option("-k", '--api-key', dest='api_key', default=None)
    parser.add_option("-i", '--interval', dest='interval', default='10')
    options, args = parser.parse_args()

    # Create the aggregator (which is the point of communication between the
    # server and reporting threads.
    aggregator = MetricsAggregator()

    # Start the reporting thread.
    reporter = Reporter(options.interval, aggregator, options.api_host, options.api_key)
    reporter.start()

    # Start the server.
    server = Server(aggregator, options.host, options.port)
    server.start()

    # If we're here, we're done.
    logger.info("Shutting down ...")


if __name__ == '__main__':
    main()

