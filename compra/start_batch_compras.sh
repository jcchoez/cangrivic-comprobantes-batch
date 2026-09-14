#!/bin/bash
source /opt/cangrivic_batch/venv/bin/activate
cd /opt/cangrivic_batch

# 1) Emitir liquidaciones de compra
python servicio_emision_liquidaciones.py       >> liquidaciones_emision.log 2>&1

# 2) Autorizar liquidaciones
python servicio_autorizacion_liquidaciones.py  >> liquidaciones_autorizacion.log 2>&1

# 3) Emitir comprobantes de retención (solo si la liquidación está AUTORIZADA)
python servicio_emision_retenciones.py         >> retenciones_emision.log 2>&1

# 4) Autorizar retenciones
python servicio_autorizacion_retenciones.py    >> retenciones_autorizacion.log 2>&1

# 5) Enviar correo al proveedor con los 2 PDFs
python servicio_envio_correo_compras.py        >> correos_compras.log 2>&1