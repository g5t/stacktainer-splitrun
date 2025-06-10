#!/usr/bin/env bash
#
# Orchestrate the communication between McStas and the ECDC stack
#
# Service              | Container             | Command
# ---------------------|-----------------------|---------
# Kakfa                | stack-tainer-kafka    | start-kafka (later, stop-kafka)
# Event Formation Unit | stack-tainer-splitrun | ${insrument} --calib ...
# Kafka-to-nexus       | stack-tainer-splitrun | launch-filewriter 
# Forwarder            | stack-tainer-splitrun | launch-forwarder [this command]
# EPICS                | stack-tainer-splitrun | mp-epics
# mcstas-EPICS         | 
# mcstas-Readout       | 
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
OPTIONS=broker:,config:,status:,help:
SHORT_OPTIONS=b:,c:,s:,h:
! PARSED=$(getopt --options=$SHORT_OPTIONS --longoptions=$OPTIONS --name "$0" -- "$@")
if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
  # getopt complained bout wrong arguments to stdout
  exit 2
fi
eval set -- "$PARSED"

[ -z ${BROKER+x} ] && broker="localhost:9092" || broker="${BROKER}"
[ -z ${CONFIG+x} ] && config="ForwardConfig" || config="${CONFIG}"
[ -z ${STATUS+x} ] && status="ForwardStatus" || status="${STATUS}"

while true; do
  case "$1" in
    -b|--broker) broker="$2"; shift 2 ;;
    -c|--config) config="$2"; shift 2 ;;
    -s|--status) status="$2"; shift 2 ;;
    -h|--help)
      echo "usage: $0 [options]"
      echo "options:"
      echo "  -h --help     print this help and exit"
      echo "  -b --broker   Kafka broker in {hostname}:{port} format, default='localhost:9092'"
      echo "  -c --config   forward configuration topic, default='ForwardConfig'"
      echo "  -s --status   forward status topic, default='ForwardStatus'"
      exit
    --) shift; break ;;
    *) echo "Programming error"; exit 3 ;;
  esac
done

# Register the *required to be predefined* Kafka topics (others can be defined on the fly later)
mp-register-topics --broker "${broker}" "${config}" "${status}"

# Start the EPICS forwarder
forwarder-launch --config-topic "${broker}/${config}"\
                 --status-topic "${broker}/${status}"\
                 --output-broker "${broker}"\
                 --skip-retrieval\
                 -v Trace
