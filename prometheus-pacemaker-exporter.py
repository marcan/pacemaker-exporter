#!/usr/bin/python3
# vim: expandtab:ts=4
#
# Copyright 2017 Hector Martin "marcan" <marcan@marcan.st>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys, subprocess, time
from socketserver import ThreadingMixIn
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET
from prometheus_client import start_http_server, REGISTRY, MetricsHandler
from prometheus_client.core import GaugeMetricFamily

def p_time(t):
    return time.mktime(time.strptime(t))

def p_bool(b):
    return 1 if b in ('true', '1') else 0

def get_xml():
    return subprocess.check_output(["crm_mon", "-X"])

class PacemakerCollector(object):
    def __init__(self, args):
        self.args = args

    def collect(self):
        xml = get_xml()
        root = ET.fromstring(xml.decode('utf-8'))

        summary = root.find('summary')
        yield GaugeMetricFamily('pacemaker_last_update', 'Last update time of cluster info',
                                value=p_time(summary.find('last_update').attrib['time']))
        yield GaugeMetricFamily('pacemaker_last_change', 'Last CIB change time',
                                value=p_time(summary.find('last_change').attrib['time']))
        yield GaugeMetricFamily('pacemaker_dc_present', 'Whether the cluster has an active DC',
                                value=p_bool(summary.find('current_dc').attrib['present']))
        yield GaugeMetricFamily('pacemaker_dc_with_quorum', 'Whether the cluster has quorum',
                                value=p_bool(summary.find('current_dc').attrib['with_quorum']))
        yield GaugeMetricFamily('pacemaker_nodes_configured', 'Number of configured nodes',
                                value=int(summary.find('nodes_configured').attrib['number']))
        yield GaugeMetricFamily('pacemaker_resources_configured', 'Number of configured resources',
                                value=int(summary.find('resources_configured').attrib['number']))
        yield GaugeMetricFamily('pacemaker_stonith_enabled', 'Whether STONITH is enabled',
                                value=p_bool(summary.find('cluster_options').attrib['stonith-enabled']))
        
        NGauge = lambda n, t: GaugeMetricFamily('pacemaker_' + n, t, labels=['node'])
        node_metrics = {
            'id':         (int,     NGauge('node_id',           'Node ID')),
            'online':     (p_bool,  NGauge('node_online',       'Node is online')),
            'standby':    (p_bool,  NGauge('node_standby',      'Node is standby')),
            'maintenance':(p_bool,  NGauge('node_maintenance',  'Node is in maintenance mode')),
            'pending':    (p_bool,  NGauge('node_pending',      'Node is pending')),
            'unclean':    (p_bool,  NGauge('node_unclean',      'Node is unclean')),
            'shutdown':   (p_bool,  NGauge('node_shutdown',     'Node is shutdown')),
            'expected_up':(p_bool,  NGauge('node_expected_up',  'Node is expected up')),
            'is_dc':      (p_bool,  NGauge('node_is_dc',        'Node is the DC')),
            'resources_running':
                          (int,     NGauge('node_resources_running',
                                           'Number of resources running on the node')),
        }

        for node in root.findall('./nodes/node'):
            for attrib, (parser, metric) in node_metrics.items():
                metric.add_metric([node.attrib['name']], parser(node.attrib[attrib]))
        
        for parser, metric in node_metrics.values():
            yield metric

        attrib_value = GaugeMetricFamily('pacemaker_node_attribute_value',
                                         'Node attribute', labels=['node', 'name'])
        attrib_expected = GaugeMetricFamily('pacemaker_node_attribute_expected',
                                            'Node attribute', labels=['node', 'name'])
        all_nodes = set()
        for node in root.findall('./node_attributes/node'):
            all_nodes.add(node.attrib['name'])
            for attrib in node.findall('attribute'):
                attrib_value.add_metric([node.attrib['name'], attrib.attrib['name']],
                                        float(attrib.attrib['value']))
                attrib_expected.add_metric([node.attrib['name'], attrib.attrib['name']],
                                           float(attrib.attrib['expected']))

        yield attrib_value
        yield attrib_expected

        RGauge = lambda n, t: GaugeMetricFamily('pacemaker_' + n, t, labels=['id', 'instance'])
        
        resource_node = GaugeMetricFamily('pacemaker_resource_node',
                                          'Whether a resource is running on each node',
                                          labels=['id', 'instance', 'node'])

        resource_elements = []
        for node in root.find('resources'):
            if node.tag == 'resource':
                resource_elements.append((True, '', node))
            elif node.tag == 'clone':
                for i, resource in enumerate(node.findall('resource')):
                    resource_elements.append((p_bool(node.attrib['unique']), str(i),  resource))

        resource_metrics = {
            'active':   (p_bool,    RGauge('resource_active',   'Resource is active')),
            'orphaned': (p_bool,    RGauge('resource_orphaned', 'Resource is orphaned')),
            'managed':  (p_bool,    RGauge('resource_managed',  'Resource is managed')),
            'failed':   (p_bool,    RGauge('resource_failed',   'Resource failed')),
            'failure_ignored':
                        (p_bool,    RGauge('resource_failure_ignored', 'Resource failure ignored')),
            'nodes_running_on':(int,RGauge('resource_nodes_running_on',
                                           'Number of nodes the resource is running on')),
        }

        for unique, idx, resource in resource_elements:
            resource_id = resource.attrib['id']
            if ':' in resource_id:
                resource_id, instance = resource_id.rsplit(':', 1)
            else:
                instance = idx
            labels = [resource_id, instance]

            for attrib, (parser, metric) in resource_metrics.items():
                metric.add_metric(labels, parser(resource.attrib[attrib]))

            left_nodes = set(all_nodes)
            for node in resource.findall('node'):
                resource_node.add_metric(labels + [node.attrib['name']], 1)
                left_nodes.remove(node.attrib['name'])

            # anonymous clones should be running on every node: do not make N^2 timeseries
            if unique and not self.args.omit_other_nodes:
                for node in left_nodes:
                    resource_node.add_metric(labels + [node], 0)

        for parser, metric in resource_metrics.values():
            yield metric

        yield resource_node

class MainHandler(MetricsHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/":
                self.send_html()
            elif path == "/xml":
                self.send_xml()
            elif path == "/metrics":
                MetricsHandler.do_GET(self)
            else:
                self.send_error(404)
        except Exception as e:
            self.send_error(500, str(e))

    def send_html(self):
        html = subprocess.check_output(["crm_mon", "-w"])
        html = html.split(b'\n\n',1)[1]
        html = html.replace(b"</body>", b'<p><a href="/metrics">Metrics</a> <a href="/xml">XML</a></p>\n</body>')
        html = html.replace(b"</head>", b'<style>body { font-family: sans-serif; }</style>')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(html)

    def send_xml(self):
        xml = get_xml()
        self.send_response(200)
        self.send_header('Content-Type', 'text/xml')
        self.end_headers()
        self.wfile.write(xml)

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    pass

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9202,
                        help="Port to listen on")
    parser.add_argument("--host", default="",
                        help="IP address or hostname to listen on")
    parser.add_argument("--omit-other-nodes",
                        help="Do not generate timeseries for nodes a resource "
                        "is *not* running on", action="store_true")
    args = parser.parse_args()

    REGISTRY.register(PacemakerCollector(args))
    httpd = ThreadingHTTPServer((args.host, args.port), MainHandler)
    httpd.serve_forever()

