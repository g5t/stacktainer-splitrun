#!/usr/bin/env bash

# Stash the location of _this_ script file to know where the implementation resides
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)

filepath="$(realpath -s "$1")"
INSTRUMENT_DIR=$(dirname "$filepath")
INSTRUMENT_FILE="$(basename "$filepath")"
# strip off the extension (there's only one, right?)
INSTRUMENT="${INSTRUMENT_FILE%.*}"

# Remove the script name and {instrument file}:
shift
export ARGUMENTS="$*"

echo "Execute instrument ${INSTRUMENT} in directory ${INSTRUMENT_DIR} with arguments ${ARGUMENTS}"

now=$(date -u +%Y%m%dT%H%M%S)
filename="${INSTRUMENT}_${now}.h5"
prefix="mcstas:"


# TODO (Maybe)
#  - Ensure that an apptainer instance with name `kafka-tainer` is running (and that we know its port)

lower_instrument="${INSTRUMENT,,}"
EFU_COMMAND=""
case "${lower_instrument}" in
  bifrost)
    { [ -z "${EFU_CONFIG+x}" ] || [ -z "${EFU_CONFIG}" ]; } && efu_config="/etc/efu/bifrost/configs/bifrost.json" || efu_config="${EFU_CONFIG}"
    { [ -z "${EFU_CALIB+x}" ] || [ -z "${EFU_CALIB}" ]; } && efu_calib="/etc/efu/bifrost/configs/bifrostnullcalib.json" || efu_calib="${EFU_CALIB}"
    { [ -z "${EFU_CMD_PORT+x}" ] || [ -z "${EFU_CMD_PORT}" ]; } && efu_cmd_port="$(ephemeral-port-reserve)" || efu_cmd_port="${EFU_CMD_PORT}"
    # Also use an ephemeral port number for the EFU UDP receiving port?
    EFU_COMMAND="bifrost -f ${efu_config} --calibration ${efu_calib} --cmdport ${efu_cmd_port}"
    ;;
  cspec | dream | freia | loki | miracles | nmx | trex) 
    echo "Untested splitrun for ${INSTRUMENT} -- setting EFU configuration paremeters is necessary"
    exit 1
    ;;
  *)
    echo "Unknown splitrun for ${INSTRUMENT}"
    exit 1
    ;;
esac
# (temporary?) hack-ish -- export the partial EFU startup command for use in *-impl, where the broker is known
export EFU_COMMAND


IMPL="${SCRIPT_DIR}/splitrun-nexus-impl"
IMPL_PARAMS=()
IMPL_PARAMS+=(-b "localhost:9092")
IMPL_PARAMS+=(-w ${INSTRUMENT_DIR})
IMPL_PARAMS+=(-c "ForwardConfig")
IMPL_PARAMS+=(-p ${prefix} --parameter-topic "SimulatedParameters")
IMPL_PARAMS+=(-o "WriterCommand" -j "WriterJob")
IMPL_PARAMS+=(--event-source "${lower_instrument}_detector" --event-topic "SimulatedEvents")
IMPL_PARAMS+=(--filename "${filename}")
IMPL_PARAMS+=(--origin "sample_stack")
if test -f "${INSTRUMENT_DIR}/${INSTRUMENT}.json"; then
  echo "Run using pre-defined structure from ${INSTRUMENT}.json"
  IMPL_PARAMS+=(--nexus-structure "${INSTRUMENT}.json")
fi
IMPL_PARAMS+=(--nexus-structure-dump "${INSTRUMENT_DIR}/${INSTRUMENT}.dump.json")

if ${IMPL} "${IMPL_PARAMS[@]}" -- "${INSTRUMENT_FILE}" ${ARGUMENTS}; then
  # Manipulate the produced file a bit:
  if test -f "${INSTRUMENT_DIR}/${filename}"; then
    # force contiguous memory layout in the generated file (removes ability to append to datasets)
    h5repack -l CONTI "${INSTRUMENT_DIR}/${filename}" "${INSTRUMENT_DIR}/${filename}.pack"
    mv "${INSTRUMENT_DIR}/${filename}.pack" "${INSTRUMENT_DIR}/${filename}"

    # Finally, add the HDF5 representation of the instrument to the file
    # copy the instrument's HDF5 representation into the file (which grows if forced into contiguous memory?!)
    mp-insert-hdf5-instr --parent ${prefix%:*} --outfile "${INSTRUMENT_DIR}/${filename}" "${INSTRUMENT_DIR}/${INSTRUMENT_FILE}"
  else
    echo "${INSTRUMENT_DIR}/${filename} does not exist. File writer failed."
  fi
else
  echo "Impl did not finish. After kafka-to-nexus exits examine output file ${INSTRUMENT_DIR}/${filename}"
fi
