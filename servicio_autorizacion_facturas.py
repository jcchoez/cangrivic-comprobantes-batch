#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import logging
import json
from typing import Tuple, Dict, Any
from datetime import datetime

import mysql.connector as mysql
from zeep import Client
from zeep.transports import Transport
from requests import Session
from zeep.exceptions import Fault

# --------------------------- CONFIGURACIÓN --------------------------- #
DB_CFG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "cangrivic_user"),
    "password": os.getenv("DB_PASS", "Cangrivic2024!"),
    "database": os.getenv("DB_NAME", "db_factura_marisco")
}

WSDL_AUTORIZACION = os.getenv(
    "SRI_WSDL_AUTORIZACION",
    "https://cel.sri.gob.ec/comprobantes-electronicos-ws/AutorizacionComprobantesOffline?wsdl",
)

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "2.0"))
SRI_TIMEOUT = float(os.getenv("SRI_TIMEOUT", "30.0"))
LIMITE_AUTORIZACION = int(os.getenv("LIMITE_AUTORIZACION", "1000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("autorizacion_sri")

# --------------------------- DB --------------------------- #
def db_conn():
    return mysql.connect(**DB_CFG)

def fetch_ventas_pendientes_autorizacion(limit: int):
    """Obtiene ventas que han sido enviadas pero no autorizadas"""
    cn = db_conn()
    try:
        cur = cn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT venta_id, clave_acceso, estado_sri, mensaje_sri
            FROM ventas 
            WHERE estado_sri IN ('RECIBIDA', 'EN PROCESO', 'PROCESANDO')
            AND (numero_autorizacion IS NULL OR numero_autorizacion = '')
            ORDER BY ultimo_intento_envio ASC
            LIMIT %s
            """,
            (limit,)
        )
        return cur.fetchall()
    finally:
        cn.close()

# Función para convertir objetos a formato serializable
def convertir_a_serializable(obj):
    """Convierte objetos no serializables (como datetime) a formato serializable"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif hasattr(obj, '__dict__'):
        return {k: convertir_a_serializable(v) for k, v in obj.__dict__.items() if not k.startswith('_')}
    elif isinstance(obj, list):
        return [convertir_a_serializable(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: convertir_a_serializable(v) for k, v in obj.items()}
    else:
        return obj

# --------------------------- SOAP --------------------------- #
def crear_cliente(wsdl_url: str) -> Client:
    session = Session()
    session.timeout = SRI_TIMEOUT
    transport = Transport(session=session)
    return Client(wsdl=wsdl_url, transport=transport)

def autorizar_comprobante_sri(clave_acceso: str) -> Tuple[str, str, str, Dict[str, Any]]:
    """
    Solicita la autorización de un comprobante al SRI usando la clave de acceso.
    Devuelve (estado, numero_autorizacion, mensaje, respuesta_json)
    """
    try:
        client = crear_cliente(WSDL_AUTORIZACION)
        logger.info(f"Solicitando autorización para clave: {clave_acceso}")
        
        resp = client.service.autorizacionComprobante(clave_acceso)

        # Debug: mostrar la estructura de la respuesta
        logger.info(f"Tipo de respuesta: {type(resp)}")
        if resp is not None:
            logger.info(f"Atributos de respuesta: {[attr for attr in dir(resp) if not attr.startswith('_')]}")
            # Para debugging adicional, podemos imprimir la respuesta como string
            logger.info(f"Respuesta como string: {str(resp)}")
        else:
            logger.warning("Respuesta del SRI es None")
            return "ERROR", "", "Respuesta nula del SRI", {}

        # Verificar si la respuesta tiene la estructura esperada
        # Basado en la respuesta XML que mostraste, la estructura es directa
        if hasattr(resp, 'autorizaciones') and resp.autorizaciones:
            autorizaciones = resp.autorizaciones.autorizacion
        else:
            logger.error("No se pudo encontrar estructura de autorizaciones en la respuesta")
            return "ERROR", "", "Estructura de respuesta inesperada del SRI", {}

        # Verificar autorizaciones
        if not autorizaciones:
            logger.error("Lista de autorizaciones vacía")
            return "ERROR", "", "Lista de autorizaciones vacía", {}

        # Normalizar: puede ser lista o único objeto
        if not isinstance(autorizaciones, list):
            autorizaciones = [autorizaciones]

        auth = autorizaciones[0]  # tomar la primera autorización

        estado = getattr(auth, "estado", "ERROR")
        numero_autorizacion = getattr(auth, "numeroAutorizacion", "")
        fecha_autorizacion = getattr(auth, "fechaAutorizacion", "")
        ambiente = getattr(auth, "ambiente", "2")
        comprobante = getattr(auth, "comprobante", None)

        # Procesar mensajes
        mensajes = []
        if hasattr(auth, "mensajes") and auth.mensajes:
            ms = auth.mensajes.mensaje
            if not isinstance(ms, list):
                ms = [ms]
            for m in ms:
                mensajes.append({
                    "identificador": getattr(m, "identificador", ""),
                    "mensaje": getattr(m, "mensaje", ""),
                    "informacionAdicional": getattr(m, "informacionAdicional", ""),
                    "tipo": getattr(m, "tipo", "")
                })

        # Construir respuesta en formato JSON
        respuesta_json = {
            "estado": estado,
            "numeroAutorizacion": numero_autorizacion,
            "fechaAutorizacion": convertir_a_serializable(fecha_autorizacion),
            "ambiente": ambiente,
            "mensajes": mensajes,
            "comprobante": comprobante is not None
        }

        # Construir un mensaje compacto para guardar en DB
        mensaje = "; ".join(
            [f"{m['identificador']}: {m['mensaje']} ({m['tipo']})"
             for m in mensajes]
        ) if mensajes else "Sin mensajes específicos"

        logger.info(f"Autorización exitosa: {estado}, Número: {numero_autorizacion}")
        return estado, numero_autorizacion, mensaje, respuesta_json

    except Fault as e:
        logger.error(f"Fault del SRI en autorización: {e}")
        return "ERROR", "", f"Fault del SRI: {str(e)}", {}
    except Exception as e:
        logger.error(f"Error inesperado en autorización: {e}")
        return "ERROR", "", f"Error inesperado: {str(e)}", {}

# --------------------------- MAIN --------------------------- #
def main():
    logger.info("Iniciando proceso de AUTORIZACIÓN de facturas al SRI...")
    ventas = fetch_ventas_pendientes_autorizacion(LIMITE_AUTORIZACION)
    
    if not ventas:
        logger.info("No hay ventas pendientes de autorización")
        return

    logger.info(f"Encontradas {len(ventas)} ventas pendientes de autorización")
    
    cn = db_conn()
    try:
        cur = cn.cursor()
        
        for venta in ventas:
            venta_id = venta['venta_id']
            clave_acceso = venta['clave_acceso']
            estado_actual = venta['estado_sri']
            
            logger.info(f"Procesando autorización para venta ID: {venta_id}, Clave: {clave_acceso}")
            
            try:
                # Solicitar autorización
                estado_auth = "ERROR"
                numero_autorizacion = ""
                mensaje_auth = "No se intentó autorización"
                respuesta_json = {}
                
                estado_auth, numero_autorizacion, mensaje_auth, respuesta_json = autorizar_comprobante_sri(clave_acceso)
                
                # Mostrar la respuesta en formato JSON exactamente como llega
                logger.info("Respuesta JSON del SRI:")
                json_str = json.dumps(respuesta_json, indent=2, ensure_ascii=False)
                logger.info(json_str)
                
                logger.info(f"Resultado Autorización: {estado_auth}")
                
                # Verificar si la columna respuesta_json existe antes de intentar actualizarla
                try:
                    cur.execute(
                        """
                        UPDATE ventas 
                        SET estado_sri=%s, 
                            mensaje_sri=CONCAT(COALESCE(mensaje_sri,''), ' | Autorización: ', %s),
                            numero_autorizacion=%s, 
                            fecha_autorizacion=NOW(),
                            respuesta_json=%s
                        WHERE venta_id=%s
                        """,
                        (estado_auth, mensaje_auth, numero_autorizacion, json_str, venta_id)
                    )
                except mysql.Error as e:
                    if e.errno == 1054:  # Error de columna desconocida
                        logger.warning("Columna respuesta_json no existe, actualizando sin ella")
                        cur.execute(
                            """
                            UPDATE ventas 
                            SET estado_sri=%s, 
                                mensaje_sri=CONCAT(COALESCE(mensaje_sri,''), ' | Autorización: ', %s),
                                numero_autorizacion=%s, 
                                fecha_autorizacion=NOW()
                            WHERE venta_id=%s
                            """,
                            (estado_auth, mensaje_auth, numero_autorizacion, venta_id)
                        )
                    else:
                        raise
                
                cn.commit()
                
                # Pausa entre autorizaciones
                time.sleep(0.5)
                
            except Exception as e:
                logger.error(f"Error procesando autorización venta {venta_id}: {e}")
                try:
                    cur.execute(
                        """
                        UPDATE ventas 
                        SET mensaje_sri=CONCAT(COALESCE(mensaje_sri,''), ' | Error autorización: ', %s)
                        WHERE venta_id=%s
                        """,
                        (str(e)[:500], venta_id)
                    )
                    cn.commit()
                except Exception as db_error:
                    logger.error(f"Error al actualizar mensaje de error en BD: {db_error}")
                
    finally:
        cn.close()
    
    logger.info("Proceso de AUTORIZACIÓN de facturas al SRI completado")

if __name__ == "__main__":
    main()