#!/usr/bin/env bash
#
# Orchestrate the communication between McStas and the ECDC stack
#
# Service              | Container
# ---------------------|------------
# Kakfa                | Confluent Kafka
# Event Formation Unit | event-formation-units:latest
# Kafka-to-nexus       | kafka-to-nexus:latest
# Forwarder            | [this container]
# EPICS                | mcstas-ecdc-plus:latest
# mcstas-EPICS         | mcstas-ecdc-plus:latest/[user provided instrument]
# mcstas-Readout       | mcstas-ecdc-plus:latest/[user provided instrument]
#

# Allow this bash script to use job controls:
set -m -o errexit -o pipefail -o noclobber -o nounset

# Ensure the required getopt for option handling is available:
! getopt --test >> /dev/null
if [[ ${PIPESTATUS[0]} -ne 4 ]]; then
  # shellcheck disable=SC2016
  echo '`getopt --test` failed in this environment. Install "enhanced getopt" on your system.'
  exit 1
fi

# {option}: == one required argument
OPTIONS=broker:,config:,status:,grafana:
SHORT_OPTIONS=b:,c:,s:,g:
! PARSED=$(getopt --options=$SHORT_OPTIONS --longoptions=$OPTIONS --name "$0" -- "$@")
if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
  # getopt complained bout wrong arguments to stdout
  exit 2
fi
eval set -- "$PARSED"

[ -z ${BROKER+x} ] && broker="broker:9092" || broker="${BROKER}"
[ -z ${CONFIG+x} ] && config="ForwardConfig" || config="${CONFIG}"
[ -z ${STATUS+x} ] && status="ForwardStatus" || status="${STATUS}"
[ -z ${GRAFANA+x} ] && grafana="grafana:2003" || grafana="${GRAFANA}"
while true; do
  case "$1" in
    -b|--broker) broker="$2"; shift 2 ;;
    -c|--config) config="$2"; shift 2 ;;
    -s|--status) status="$2"; shift 2 ;;
    -g|--grafana) grafana="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "Programming error"; exit 3 ;;
  esac
done

# Register the *required to be predefined* Kafka topics (others can be defined on the fly later)
mp-register-topics --broker "${broker}" "${config}" "${status}"

# Start the EPICS forwarder
forwarder-launch --config-topic "${broker}/${config}"\
                 --status-topic "${broker}/${status}"\
                 --output-broker "${broker}" --skip-retrieval --grafana-carbon-address "${grafana}"\
                 -v Trace