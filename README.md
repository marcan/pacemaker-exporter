# Prometheus pacemaker exporter

Export your Pacemaker cluster status to Prometheus

## Requirements

* Python 3
* [Prometheus Python client](https://github.com/prometheus/client_python)
* crm_mon

## Usage

No packaging (yet).

    $ ./prometheus-pacemaker-exporter.py

Browse to http://127.0.0.1:9202/metrics for the Prometheus metrics. You also
get a nice HTML cluster status page at http://127.0.0.1:9202/ for free.

