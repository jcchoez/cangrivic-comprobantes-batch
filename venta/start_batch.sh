#!/bin/bash
source /opt/cangrivic_batch/venv/bin/activate
python /opt/cangrivic_batch/servicio_emision_facturas.py >> /opt/cangrivic_batch/emision.log 2>&1
python /opt/cangrivic_batch/servicio_autorizacion_facturas.py >> /opt/cangrivic_batch/autorizacion.log 2>&1
python /opt/cangrivic_batch/servicio_envio_correo_masivo.py >> /opt/cangrivic_batch/correo.log 2>&1
