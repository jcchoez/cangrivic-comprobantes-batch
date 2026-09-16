#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, decimal, logging, base64, traceback
from dataclasses import dataclass
from typing import List, Tuple, Optional
from datetime import datetime, timezone

import mysql.connector as mysql
from lxml import etree
from zeep import Client
from zeep.transports import Transport
from requests import Session
from zeep.exceptions import Fault

from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.backends import default_backend

from signxml import methods, SignatureMethod, DigestAlgorithm, CanonicalizationMethod
from signxml.xades import XAdESSigner

logger = logging.getLogger("compras_sri")




DB_CFG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASS", "Juan1997.."),
    "database": os.getenv("DB_NAME", "db_factura_marisco")
}

WSDL_RECEPCION = os.getenv(
    "SRI_WSDL_RECEPCION",
    "https://celcer.sri.gob.ec/comprobantes-electronicos-ws/RecepcionComprobantesOffline?wsdl",
)

WSDL_AUTORIZACION = os.getenv(
    "SRI_WSDL_AUTORIZACION",
    "https://celcer.sri.gob.ec/comprobantes-electronicos-ws/AutorizacionComprobantesOffline?wsdl",
)


"""
DB_CFG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "cangrivic_user"),
    "password": os.getenv("DB_PASS", "Cangrivic2024!"),
    "database": os.getenv("DB_NAME", "db_factura_marisco"),
}


WSDL_RECEPCION = os.getenv(
    "SRI_WSDL_RECEPCION",
    "https://cel.sri.gob.ec/comprobantes-electronicos-ws/RecepcionComprobantesOffline?wsdl",
)
WSDL_AUTORIZACION = os.getenv(
    "SRI_WSDL_AUTORIZACION",
    "https://cel.sri.gob.ec/comprobantes-electronicos-ws/AutorizacionComprobantesOffline?wsdl",
)"""
AMBIENTE       = int(os.getenv("SRI_AMBIENTE", "1"))   # 1=Pruebas, 2=Prod
TIPO_EMISION   = 1
SRI_TIMEOUT    = float(os.getenv("SRI_TIMEOUT", "30.0"))

# Retención: 1% con código 312A (según ejemplo del SRI)
PORC_RETENCION = decimal.Decimal(os.getenv("PORC_RETENCION", "1.0"))
COD_RETENCION  = os.getenv("COD_RETENCION", "312A")

# ---------- Modelos ----------
@dataclass
class Empresa:
    empresa_id: int
    ruc: str
    nombre: str
    direccion: Optional[str]
    cert_path: Optional[str]
    cert_password: Optional[str]
    establecimiento: str = "001"
    punto_emision: str = "100"
    obligado_contabilidad: str = "NO"

@dataclass
class Proveedor:
    proveedor_id: int
    identificacion: str
    nombre: str
    direccion: Optional[str]
    telefono: Optional[str]
    correo: Optional[str]
    tipo_identificacion: str = "05"

@dataclass
class Item:
    codigo: str
    descripcion: str
    cantidad: decimal.Decimal
    precio_unitario: decimal.Decimal
    subtotal: decimal.Decimal
    descuento: decimal.Decimal = decimal.Decimal("0.00")

@dataclass
class Compra:
    compra_id: int
    fecha: str
    total: decimal.Decimal
    secuencial: int
    empresa: Empresa
    proveedor: Proveedor
    items: List[Item]
    codigo_numerico: str
    establecimiento: str
    punto_emision: str
    tipo_comprobante: str = "03"
    clave_acceso_liquidacion: Optional[str] = None
    secuencial_retencion: Optional[int] = None
    codigo_numerico_retencion: Optional[str] = None

# ---------- DB ----------
def db_conn():
    return mysql.connect(**DB_CFG)

def determinar_tipo_identificacion(identificacion: str) -> str:
    identificacion = (identificacion or "").strip()
    if len(identificacion) == 13 and identificacion.isdigit():
        return "04"
    if len(identificacion) == 10 and identificacion.isdigit():
        return "05"
    if identificacion.isalpha() and len(identificacion) <= 20:
        return "06"
    if identificacion.isdigit() and len(identificacion) < 10:
        return "07"
    return "08"

def fetch_empresa(cur, empresa_id: int) -> Empresa:
    cur.execute("""
        SELECT empresa_id, ruc, empresa_nombre, empresa_direccion,
               cert_path, cert_password, establecimiento, punto_emision,
               obligado_contabilidad
        FROM empresa WHERE empresa_id=%s
    """, (empresa_id,))
    r = cur.fetchone()
    if not r:
        raise RuntimeError(f"Empresa {empresa_id} no encontrada")
    return Empresa(
        empresa_id=r[0], ruc=r[1], nombre=r[2], direccion=r[3],
        cert_path=r[4], cert_password=r[5],
        establecimiento=r[6] or "001", punto_emision=r[7] or "100",
        obligado_contabilidad=r[8] or "NO",
    )

def fetch_proveedor(cur, proveedor_id: int) -> Proveedor:
    cur.execute("""
        SELECT proveedor_id, proveedor_identificacion, proveedor_nombre,
               proveedor_direccion, proveedor_telefono, proveedor_correo
        FROM proveedor WHERE proveedor_id=%s
    """, (proveedor_id,))
    r = cur.fetchone()
    if not r:
        raise RuntimeError(f"Proveedor {proveedor_id} no encontrado")
    return Proveedor(
        proveedor_id=r[0], identificacion=str(r[1]), nombre=str(r[2]),
        direccion=r[3], telefono=r[4], correo=r[5],
        tipo_identificacion=determinar_tipo_identificacion(str(r[1])),
    )

def fetch_compra_items(cur, compra_id: int) -> List[Item]:
    cur.execute("""
        SELECT i.cantidad, i.precio_unitario, i.subtotal,
               p.producto_codigo,
               COALESCE(p.producto_descripcion, p.producto_nombre)
        FROM compra_items i
        JOIN producto p ON p.producto_id = i.producto_id
        WHERE i.compra_id=%s
    """, (compra_id,))
    return [
        Item(
            codigo=str(cod or ""),
            descripcion=str(desc or cod or ""),
            cantidad=decimal.Decimal(cant),
            precio_unitario=decimal.Decimal(pu),
            subtotal=decimal.Decimal(sub),
        )
        for (cant, pu, sub, cod, desc) in cur.fetchall()
    ]

def siguiente_secuencial_retencion(cur, empresa_id: int) -> int:
    cur.execute("""
        SELECT COALESCE(MAX(CAST(secuencial_retencion AS UNSIGNED)), 0)
        FROM compras WHERE empresa_id=%s
    """, (empresa_id,))
    return int(cur.fetchone()[0]) + 1

# ---------- Clave de acceso ----------
def modulo11(numero: str) -> int:
    factores = [2, 3, 4, 5, 6, 7]
    acc = 0
    for i, ch in enumerate(reversed(numero)):
        acc += int(ch) * factores[i % len(factores)]
    dig = 11 - (acc % 11)
    if dig == 11: return 0
    if dig == 10: return 1
    return dig

def generar_clave_acceso(fecha_ddmmaaaa, tipo_comprobante, ruc, ambiente,
                         establecimiento, punto_emision, secuencial,
                         cod_num, tipo_emision) -> str:
    serie = f"{establecimiento}{punto_emision}"
    base = (f"{fecha_ddmmaaaa}{tipo_comprobante}{ruc}{ambiente}"
            f"{serie}{secuencial:09d}{int(cod_num):08d}{tipo_emision}")
    return base + str(modulo11(base))

# ---------- Firma ----------
def validar_certificado(cert_path: str, cert_pass: str) -> bool:
    try:
        with open(cert_path, "rb") as f:
            p12 = f.read()
        pk, cert, _ = pkcs12.load_key_and_certificates(
            p12, cert_pass.encode() if cert_pass else None, default_backend()
        )
        if not pk or not cert:
            return False
        nva = cert.not_valid_after
        now = datetime.utcnow() if nva.tzinfo is None else datetime.now(timezone.utc)
        return nva >= now
    except Exception as e:
        logger.error(f"Certificado inválido: {e}")
        return False

def firmar_xml(xml_sin_firma: str, cert_path: str, cert_pass: str) -> str:
    """Firma XAdES-BES con RSA-SHA256 (requerido por el SRI actual)."""
    if not cert_path or not cert_pass:
        raise ValueError("Certificado no proporcionado.")
    with open(cert_path, "rb") as f:
        p12 = f.read()
    private_key, cert, extras = pkcs12.load_key_and_certificates(
        p12, cert_pass.encode() if cert_pass else None, default_backend()
    )
    if not private_key or not cert:
        raise RuntimeError("No se pudo extraer clave/cert del PKCS12")
    chain = [cert] + (list(extras) if extras else [])

    signer = XAdESSigner(
        method=methods.enveloped,
        signature_algorithm=SignatureMethod.RSA_SHA256,
        digest_algorithm=DigestAlgorithm.SHA256,
        c14n_algorithm=CanonicalizationMethod.CANONICAL_XML_1_0,
    )
    root = etree.fromstring(xml_sin_firma.encode("utf-8"))
    signed = signer.sign(
        root, key=private_key, cert=chain,
        reference_uri="#comprobante", id_attribute="id",
    )
    return etree.tostring(signed, encoding="UTF-8", xml_declaration=True).decode("utf-8")

# ---------- SOAP ----------
def crear_cliente(wsdl: str) -> Client:
    s = Session(); s.timeout = SRI_TIMEOUT
    return Client(wsdl=wsdl, transport=Transport(session=s))

def enviar_comprobante_sri(xml_firmado: str) -> Tuple[str, str]:
    try:
        client = crear_cliente(WSDL_RECEPCION)
        b64 = base64.b64encode(xml_firmado.encode("utf-8")).decode("utf-8")
        resp = client.service.validarComprobante(b64)
        if resp and hasattr(resp, "estado"):
            msg = str(getattr(resp, "comprobantes", "")) or "Sin mensaje"
            return resp.estado, msg
        return "ERROR", "Respuesta inválida del SRI"
    except Fault as e:
        return "ERROR", f"Fault: {e}"
    except Exception as e:
        return "ERROR", f"Error: {e}"

def _serializable(o):
    if isinstance(o, datetime): return o.isoformat()
    if hasattr(o, "__dict__"): return {k: _serializable(v) for k, v in o.__dict__.items() if not k.startswith("_")}
    if isinstance(o, list): return [_serializable(i) for i in o]
    if isinstance(o, dict): return {k: _serializable(v) for k, v in o.items()}
    return o

def autorizar_comprobante_sri(clave_acceso: str):
    try:
        client = crear_cliente(WSDL_AUTORIZACION)
        resp = client.service.autorizacionComprobante(clave_acceso)
        if not resp or not getattr(resp, "autorizaciones", None):
            return "ERROR", "", "Sin autorizaciones", {}
        auts = resp.autorizaciones.autorizacion
        if not isinstance(auts, list): auts = [auts]
        a = auts[0]
        estado = getattr(a, "estado", "ERROR")
        numero = getattr(a, "numeroAutorizacion", "") or ""
        fecha  = getattr(a, "fechaAutorizacion", "")
        mensajes = []
        if getattr(a, "mensajes", None):
            ms = a.mensajes.mensaje
            if not isinstance(ms, list): ms = [ms]
            for m in ms:
                mensajes.append({
                    "identificador": getattr(m, "identificador", ""),
                    "mensaje": getattr(m, "mensaje", ""),
                    "informacionAdicional": getattr(m, "informacionAdicional", ""),
                    "tipo": getattr(m, "tipo", ""),
                })
        rj = {"estado": estado, "numeroAutorizacion": numero,
              "fechaAutorizacion": _serializable(fecha), "mensajes": mensajes}
        msg = "; ".join(f"{m['identificador']}: {m['mensaje']}" for m in mensajes) or "Sin mensajes"
        return estado, numero, msg, rj
    except Fault as e:
        return "ERROR", "", f"Fault: {e}", {}
    except Exception as e:
        return "ERROR", "", f"Error: {e}", {}