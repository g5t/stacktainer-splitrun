#!/usr/bin/env bash
#
# Orchestrate the communication between McStas and the ECDC stack
#
# Service              | Container
# ---------------------|------------
# Kafka                | Confluent Kafka
# Event Formation Unit | event-formation-units:latest
# Kafka-to-nexus       | kafka-to-nexus:latest
# Forwarder            | epics-to-kafka:latest
# EPICS                | [via McCode-plumber]
# mcstas-EPICS         | [user provided instrument]
# mcstas-Readout       | [user provided instrument]
#

# Allow this bash script to use job controls:
set -m -o errexit -o noclobber -o nounset

# {option}: == one required argument
OPTIONS=broker:,work:,prefix:,config:,status:,command:,job:,event-source:,event-topic:,parameter-topic:,title:,filename:,nexus-structure:,monitor-source:,origin:,write-after,nexus-structure-dump:
SHORT_OPTIONS=b:,w:,p:,c:,s:,o:,j:,t:,f:,n:,m:,r:,d:
PARSED=$(getopt -o $SHORT_OPTIONS -l $OPTIONS --name "$0" -- "$@") || exit 1
eval set -- "$PARSED"

[ -z ${BROKER+x} ] && broker="localhost:9092" || broker="${BROKER}"
[ -z ${WORK_DIR+x} ] && work_dir=$(pwd) || work_dir="${WORK_DIR}"
[ -z ${EPICS_PREFIX+x} ] && epics_prefix="mcstas:" || epics_prefix="${EPICS_PREFIX}"
[ -z ${FORWARDER_CONFIG+x} ] && forwarder_config="ForwardConfig" || forwarder_config="${FORWARDER_CONFIG}"
[ -z ${FORWARDER_STATUS+x} ] && forwarder_status="ForwardStatus" || forwarder_status="${FORWARDER_STATUS}"
[ -z ${WRITER_COMMAND+x} ] && writer_command="WriterCommand" || writer_command="${WRITER_COMMAND}"
[ -z ${WRITER_JOB+x} ] && writer_job="WriterJob" || writer_job="${WRITER_JOB}"
[ -z ${EVENT_SOURCE+x} ] && event_source="bifrost_detector" || event_source="${EVENT_SOURCE}"
[ -z ${EVENT_TOPIC+x} ] && event_topic="SimulatedEvents" || event_topic="${EVENT_TOPIC}"
[ -z ${PARAMETER_TOPIC+x} ] && parameter_topic="SimulatedParameter" || parameter_topic="${PARAMETER_TOPIC}"
[ -z ${TITLE+x} ] && title="" || title="${TITLE}"
[ -z ${FILENAME+x} ] && filename="" || filename="${FILENAME}"
[ -z ${STRUCTURE_FILE+x} ] && structure_file="" || structure_file="${STRUCTURE_FILE}"
[ -z ${STRUCTURE_OUT+x} ] && structure_out="" || structure_out="${STRUCTURE_OUT}"
[ -z ${MONITOR_SOURCE+x} ] && monitor_source="mccode-to-kafka" || monitor_source="${MONITOR_SOURCE}"
[ -z ${ORIGIN+x} ] && origin="" || origin="${ORIGIN}"
[ -z ${WRITE_AFTER+x} ] && write_after=false || write_after=${WRITE_AFTER}
while true; do
  case "$1" in
    -b|--broker) broker="$2"; shift 2 ;;
    -w|--work) work_dir="$2"; shift 2 ;;
    -p|--prefix) epics_prefix="$2"; shift 2 ;;
    -c|--config) forwarder_config="$2"; shift 2 ;;
    -s|--status) forwarder_status="$2"; shift 2 ;;
    -o|--command) writer_command="$2"; shift 2 ;;
    -j|--job) writer_job="$2"; shift 2 ;;
    --event-source) event_source="$2"; shift 2 ;;
    --event-topic) event_topic="$2"; shift 2 ;;
    --parameter-topic) parameter_topic="$2"; shift 2 ;;
    -t|--title) title="$2"; shift 2 ;;
    -f|--filename) filename="$2"; shift 2 ;;
    -n|--nexus-structure) structure_file="$2"; shift 2 ;;
    -d|--nexus-structure-dump) structure_out="$2"; shift 2 ;;
    -m|--monitor-source) monitor_source="$2"; shift 2 ;;
    -r|--origin) origin="$2"; shift 2 ;;
    --write-after) write_after=true; ;;
    --) shift; break ;;
    *) echo "Programming error"; exit 3 ;;
  esac
done

if [ ! -d "${work_dir}" ]
then
  echo "The requested working directory ${work_dir} does not exist"
  exit 1
else
  cd ${work_dir} || exit
fi

now=$(date -u +%Y%m%dT%H%M%S)  # YYYYMMDDTHHMMSS -- no escape-required separators

# If title is unset or an empty string, make a title
{ [ -z "${title+x}" ] || [ -z "${title}" ]; } && title="\"splitrun ${*}\""
# if the filename is unset or an empty string, construct a filename from the current time
{ [ -z "${filename+x}" ] || [ -z "${filename}" ]; } && filename="${1%.*}_${now}.h5"


echo "Running entrypoint script from $(pwd)"

function er {
  echo "-------------------"
  printf "\t%s\n" "$*"
  "${@}"
}

printf "======\nStart the EFU\n======\n"
${EFU_COMMAND} -b "${broker}" > "${now}_efu.log" 2>&1 &
efu_pid=$!

printf "======\nStart the forwarder service\n======\n"
launch-forwarder --broker "${broker}" --config "${forwarder_config}" --status "${forwarder_status}" &
forwarder_pid=$!

printf "======\nStart the file writer service\n======\n"
launch-writer --broker "${broker}" --work "${work_dir}" --command "${writer_command}" --job "${writer_job}" &
writer_pid=$!

# Discover the instrument EPICS parameters from the mcstas instr/c/out file, and launch a caproto EPICS server
printf "======\nStart EPICS\n======\n"
mp-epics "$1" --prefix "${epics_prefix}" > "${now}_epics.log" 2>&1 &

# Register the *required to be predefined* Kafka topics (others can be defined on the fly later)
printf "=====\n(Pre)Register Kafka topics\n"
mp-register-topics -q --broker "${broker}" "${parameter_topic}" "${event_topic}"
# This list needs to be supplemented by the da00 topics if the file-writer is started before the simulation
# Take the simplest route, and use the JSON file if it was provided
if { [ -z "${structure_file+x}" ] || [ -z "${structure_file}" ]; }; then
  echo "Can not register da00 topics without providing the JSON structure file"
  json_topics=()
else
  json_topics=($(grep "\s*\"topic\"" "${structure_file}" | sed 's/\s*"topic":\s*"\([^"]*\)".*/\1/' | uniq))
  mp-register-topics -q --broker "${broker}" "${json_topics[@]}"
fi


printf "=====\nInform the forwarder what it should forward\n"
# Configure the forwarder to use the EPICS PVs
mp-forwarder-setup "$1" --prefix "${epics_prefix}" --config "${broker}/${forwarder_config}" --topic "${parameter_topic}"


# Tell the writer to write, starting now
start_time=$(mp-writer-from)
# Generate a UUID (via apt package 'uuid' provided 'uuid' utility)
job_id=$(uuid)
# Collect the writer arguments:
WRITER_PARAMS=("$1" --prefix "${epics_prefix}" --broker "${broker}"\
               --job "${writer_job}" --command "${writer_command}" --topic "${parameter_topic}"\
               --event-source "${event_source}" --event-topic "${event_topic}" \
               --title "${title}" --filename "${filename}" --ns-file "${structure_file}"\
               --start-time "${start_time}" --origin "${origin}" --job-id "${job_id}")

if [[ ! -z ${structure_out} ]]; then
  WRITER_PARAMS+=(--ns-save "${structure_out}")
fi

status=0

if [ "${write_after}" == false ]; then
  printf "=====\nRun the file writer for a file from now (do not wait for completion)\n"
  if ! mp-writer-write "${WRITER_PARAMS[@]}"; then
    printf "The file-writer did not start?!\n"
    status=1;
  fi
fi

if [ "${status}" -eq "0" ]; then
  printf "=====\nStart splitrun\n"
  er mp-splitrun --parallel --broker "${broker}" --source "${monitor_source}" ${json_topics[@]/#/--topic } "$@"  # the version with the monitor-forwarding callback

  if [ "${write_after}" == true ]; then
    printf "=====\nRun the file writer for a file from ${start_time} until now (writing starts now and takes some time)\n"
    mp-writer-write "$1" "${WRITER_PARAMS[@]}" --wait
  fi
fi

# Even if status is 1, try and wait on stopping the writer.
if [ "${write_after}" == false ]; then
  printf "====\nWait for the file writer to finish\n"
  WAIT_PARAMS=(--broker "${broker}" --job "${writer_job}" --command "${writer_command}" "${job_id}" -t 60)
  if ! mp-writer-wait "${WAIT_PARAMS[@]}"; then
    status=1;
  fi
fi

if [ "$status}" -eq "1" ]; then
  printf "===\nWriting failed, attempt killing the job (use mp-writer-list to check its status)\n"
  # the empty string "" could be replaced by the writer process id, if we knew it
  KILL_PARAMS=(--broker "${broker}" --topic "${writer_job}" --command "${writer_command}" "" "${job_id}")
  er mp-writer-kill "${KILL_PARAMS[@]}"
fi

# Tell the forwarder to stop forwarding these 'PVs'
mp-forwarder-teardown "$1" --prefix "${epics_prefix}" --config "${broker}/${forwarder_config}" --topic "${parameter_topic}"

# Tell the EPICS server to stop
kill %mp-epics
# Tell the file writer service to stop
kill $writer_pid
# Tell the forwarder service to stop
kill $forwarder_pid
# Tell the EFU to stop
kill $efu_pid


exit $status
