#!/usr/bin/env python3
# -- coding: utf-8 --

import os
import time
import decimal
import logging
from dataclasses import dataclass
from typing import List, Tuple
import base64
import re
import xml.etree.ElementTree as ET
import mysql.connector as mysql
from lxml import etree
from zeep import Client
from zeep.transports import Transport
from requests import Session
from zeep.exceptions import Fault

# --- Cryptography para firmar PKCS12 ---
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography import x509

# --- Librería para firma XML (SignXML) ---
from signxml import methods, SignatureMethod, DigestAlgorithm, CanonicalizationMethod, namespaces
from signxml.xades import XAdESSigner

# --------------------------- CONFIGURACIÓN --------------------------- #

DB_CFG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "cangrivic_user"),
    "password": os.getenv("DB_PASS", "Cangrivic2024!"),
    "database": os.getenv("DB_NAME", "db_factura_marisco")
}

AMBIENTE = int(os.getenv("SRI_AMBIENTE", "2"))  # 1=Pruebas, 2=Producción
TIPO_EMISION = 1
WSDL_RECEPCION = os.getenv(
    "SRI_WSDL_RECEPCION",
    #"https://celcer.sri.gob.ec/comprobantes-electronicos-ws/RecepcionComprobantesOffline?wsdl",
    "https://cel.sri.gob.ec/comprobantes-electronicos-ws/RecepcionComprobantesOffline?wsdl",
)

MAX_DOCS_POR_LOTE = 50
MAX_TAM_LOTE_BYTES = 500 * 1024
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "2.0"))
SRI_TIMEOUT = float(os.getenv("SRI_TIMEOUT", "30.0"))
LIMITE_ENVIO = int(os.getenv("LIMITE_ENVIO", "1000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("emision_sri")

# --------------------------- DIRECTORIO DE FACTURAS --------------------------- #

DIR_FACTURAS = os.path.join(os.getcwd(), "facturas")
os.makedirs(DIR_FACTURAS, exist_ok=True)

# --------------------------- MODELOS --------------------------- #

@dataclass
class Empresa:
    empresa_id: int
    ruc: str
    nombre: str
    direccion: str | None
    nombre_comercial: str | None = None
    contribuyente_especial: str | None = None
    cert_path: str | None = None
    cert_password: str | None = None
    establecimiento: str = "001"
    punto_emision: str = "001"
    obligado_contabilidad: str = "NO"

@dataclass
class Cliente:
    identificacion: str
    nombre: str
    direccion: str | None
    telefono: str | None
    tipo_identificacion: str = "04"
    guia_remision: str | None = None
    correo: str | None = None

@dataclass
class Item:
    codigo: str
    descripcion: str
    cantidad: decimal.Decimal
    precio_unitario: decimal.Decimal
    subtotal: decimal.Decimal
    descuento: decimal.Decimal = decimal.Decimal("0.00")
    codigo_auxiliar: str | None = None

@dataclass
class Venta:
    venta_id: int
    fecha: str
    total: decimal.Decimal
    secuencial: int
    empresa: Empresa
    cliente: Cliente
    items: List[Item]
    codigo_numerico: str
    establecimiento: str
    punto_emision: str
    tipo_comprobante: str = "01"
    propina: decimal.Decimal = decimal.Decimal("0.00")
    valor_ret_iva: decimal.Decimal | None = None
    valor_ret_renta: decimal.Decimal | None = None

# --------------------------- DB --------------------------- #

def db_conn():
    return mysql.connect(**DB_CFG)

def determinar_tipo_identificacion(identificacion: str) -> str:
    identificacion = identificacion.strip()
    if len(identificacion) == 13 and identificacion.isdigit():
        return "04"  # RUC
    elif len(identificacion) == 10 and identificacion.isdigit():
        return "05"  # Cédula
    elif len(identificacion) <= 15:
        return "06"  # Pasaporte
    else:
        return "07"  # Ventas a consumidor final

def fetch_empresa(cursor, empresa_id: int) -> Empresa:
    cursor.execute(
        """
        SELECT empresa_id, ruc, empresa_nombre, empresa_direccion, 
               cert_path, cert_password, establecimiento, punto_emision, 
               obligado_contabilidad
        FROM empresa 
        WHERE empresa_id=%s
        """,
        (empresa_id,),
    )
    r = cursor.fetchone()
    if not r:
        raise RuntimeError(f"Empresa {empresa_id} no encontrada")
    return Empresa(
        empresa_id=r[0],
        ruc=r[1],
        nombre=r[2],
        direccion=r[3],
        cert_path=r[4],
        cert_password=r[5],
        establecimiento=r[6] or "001",
        punto_emision=r[7] or "100",
        obligado_contabilidad=r[8] or "NO",
        contribuyente_especial=None,
        nombre_comercial=None
    )

def fetch_ventas_pendientes_envio(limit: int) -> List[Venta]:
    cn = db_conn()
    try:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT v.venta_id, v.fecha_venta, v.total, v.empresa_id, 
                   v.secuencial, v.codigo_numerico, v.establecimiento, 
                   v.punto_emision, c.cliente_identificacion, c.cliente_nombre, 
                   c.cliente_direccion, c.cliente_telefono, c.cliente_correo
            FROM ventas v 
            JOIN cliente c ON c.cliente_id = v.cliente_id 
            WHERE COALESCE(v.estado_sri,'') NOT IN ('AUTORIZADO','ENVIADO','RECIBIDA')
            ORDER BY v.fecha_venta ASC 
            LIMIT %s
            """,
            (limit,)
        )
        ventas = []
        for row in cur.fetchall():
            (venta_id, fecha_venta, total, empresa_id, secuencial, 
             codigo_numerico, establecimiento, punto_emision, 
             ci, cnombre, cdirec, ctel, ccorreo) = row
            if hasattr(fecha_venta, 'strftime'):
                fecha_str = fecha_venta.strftime("%Y-%m-%d %H:%M:%S")
            else:
                fecha_str = str(fecha_venta)
            emp = fetch_empresa(cur, empresa_id)
            tipo_identificacion = determinar_tipo_identificacion(ci)
            cur_items = cn.cursor()
            cur_items.execute(
                """
                SELECT i.cantidad, i.precio_unitario, i.subtotal, 
                       p.producto_codigo, COALESCE(p.producto_descripcion, p.producto_nombre)
                FROM venta_items i 
                JOIN producto p ON p.producto_id = i.producto_id 
                WHERE i.venta_id=%s
                """,
                (venta_id,)
            )
            items = []
            for (cant, pu, sub, cod, desc) in cur_items.fetchall():
                items.append(Item(
                    codigo=str(cod),
                    descripcion=str(desc or cod),
                    cantidad=decimal.Decimal(cant),
                    precio_unitario=decimal.Decimal(pu),
                    subtotal=decimal.Decimal(sub),
                    descuento=decimal.Decimal("0.00"),
                    codigo_auxiliar=None
                ))
            cliente = Cliente(
                identificacion=str(ci),
                nombre=str(cnombre),
                direccion=str(cdirec) if cdirec else None,
                telefono=str(ctel) if ctel else None,
                tipo_identificacion=tipo_identificacion,
                guia_remision=None,
                correo=str(ccorreo) if ccorreo else None
            )
            ventas.append(Venta(
                venta_id=int(venta_id),
                fecha=fecha_str,
                total=decimal.Decimal(total),
                secuencial=int(secuencial),
                empresa=emp,
                cliente=cliente,
                items=items,
                codigo_numerico=str(codigo_numerico),
                establecimiento=establecimiento or emp.establecimiento,
                punto_emision=punto_emision or emp.punto_emision,
                propina=decimal.Decimal("0.00"),
                valor_ret_iva=None,
                valor_ret_renta=None
            ))
        return ventas
    finally:
        cn.close()

# --------------------------- CLAVE ACCESO --------------------------- #

def modulo11(numero: str) -> int:
    factores = [2,3,4,5,6,7]
    acc = 0
    for i, ch in enumerate(reversed(numero)):
        acc += int(ch) * factores[i % len(factores)]
    dig = 11 - (acc % 11)
    if dig == 11:
        return 0
    if dig == 10:
        return 1
    return dig

def generar_clave_acceso(fecha_ddmmaaaa: str, tipo_comprobante: str, ruc: str, 
                         ambiente: int, establecimiento: str, punto_emision: str, 
                         secuencial: int, cod_num: str, tipo_emision: int) -> str:
    serie = f"{establecimiento}{punto_emision}"
    base = f"{fecha_ddmmaaaa}{tipo_comprobante}{ruc}{ambiente}{serie}{secuencial:09d}{int(cod_num):08d}{tipo_emision}"
    dv = modulo11(base)
    return base + str(dv)

# --------------------------- XML FACTURA --------------------------- #

def _et(tag: str, text: str | None = None, **attrs):
    el = etree.Element(tag, **{k:str(v) for k,v in attrs.items() if v is not None})
    if text is not None:
        el.text = str(text)
    return el

def factura_xml(v: Venta, version: str = "1.1.0") -> Tuple[str, str]:
    fecha_parte = v.fecha.split()[0]
    fecha_ddmmaaaa = fecha_parte[8:10] + fecha_parte[5:7] + fecha_parte[0:4]
    fecha_emision = f"{fecha_parte[8:10]}/{fecha_parte[5:7]}/{fecha_parte[0:4]}"
    clave = generar_clave_acceso(
        fecha_ddmmaaaa, v.tipo_comprobante, v.empresa.ruc, AMBIENTE,
        v.establecimiento, v.punto_emision, v.secuencial,
        v.codigo_numerico, TIPO_EMISION
    )
    root = etree.Element("factura", id="comprobante", version=version)
    infoTrib = etree.SubElement(root, "infoTributaria")
    etree.SubElement(infoTrib, "ambiente").text = str(AMBIENTE)
    etree.SubElement(infoTrib, "tipoEmision").text = str(TIPO_EMISION)
    etree.SubElement(infoTrib, "razonSocial").text = v.empresa.nombre[:300]
    etree.SubElement(infoTrib, "ruc").text = v.empresa.ruc
    etree.SubElement(infoTrib, "claveAcceso").text = clave
    etree.SubElement(infoTrib, "codDoc").text = v.tipo_comprobante
    etree.SubElement(infoTrib, "estab").text = v.establecimiento
    etree.SubElement(infoTrib, "ptoEmi").text = v.punto_emision
    etree.SubElement(infoTrib, "secuencial").text = f"{v.secuencial:09d}"
    if v.empresa.direccion:
        etree.SubElement(infoTrib, "dirMatriz").text = v.empresa.direccion[:300]
    etree.SubElement(infoTrib, "contribuyenteRimpe").text = "CONTRIBUYENTE NEGOCIO POPULAR - RÉGIMEN RIMPE"
    infoFactura = etree.SubElement(root, "infoFactura")
    etree.SubElement(infoFactura, "fechaEmision").text = fecha_emision
    if v.empresa.direccion:
        etree.SubElement(infoFactura, "dirEstablecimiento").text = v.empresa.direccion[:300]
    etree.SubElement(infoFactura, "obligadoContabilidad").text = v.empresa.obligado_contabilidad
    etree.SubElement(infoFactura, "tipoIdentificacionComprador").text = v.cliente.tipo_identificacion
    etree.SubElement(infoFactura, "razonSocialComprador").text = v.cliente.nombre[:300]
    if v.cliente.tipo_identificacion == "04":
        etree.SubElement(infoFactura, "identificacionComprador").text = v.cliente.identificacion[:13]
    elif v.cliente.tipo_identificacion == "05":
        etree.SubElement(infoFactura, "identificacionComprador").text = v.cliente.identificacion[:10]
    else:
        etree.SubElement(infoFactura, "identificacionComprador").text = v.cliente.identificacion[:20]
    if v.cliente.direccion:
        etree.SubElement(infoFactura, "direccionComprador").text = v.cliente.direccion[:300]
    total_sin_impuestos = sum([item.cantidad * item.precio_unitario for item in v.items])
    total_descuento = sum([item.descuento for item in v.items])
    etree.SubElement(infoFactura, "totalSinImpuestos").text = f"{total_sin_impuestos:.2f}"
    etree.SubElement(infoFactura, "totalDescuento").text = f"{total_descuento:.2f}"
    total_con_impuestos = etree.SubElement(infoFactura, "totalConImpuestos")
    base_imponible_iva = total_sin_impuestos - total_descuento
    total_impuesto = etree.SubElement(total_con_impuestos, "totalImpuesto")
    etree.SubElement(total_impuesto, "codigo").text = "2"
    etree.SubElement(total_impuesto, "codigoPorcentaje").text = "0"
    etree.SubElement(total_impuesto, "baseImponible").text = f"{base_imponible_iva:.2f}"
    etree.SubElement(total_impuesto, "valor").text = "0.00"
    importe_total = base_imponible_iva + v.propina
    etree.SubElement(infoFactura, "propina").text = f"{v.propina:.2f}" if v.propina else "0.00"
    etree.SubElement(infoFactura, "importeTotal").text = f"{importe_total:.2f}"
    etree.SubElement(infoFactura, "moneda").text = "DOLAR"
    pagos = etree.SubElement(infoFactura, "pagos")
    pago = etree.SubElement(pagos, "pago")
    etree.SubElement(pago, "formaPago").text = "01"
    etree.SubElement(pago, "total").text = f"{importe_total:.2f}"
    if v.valor_ret_iva:
        etree.SubElement(infoFactura, "valorRetIva").text = f"{v.valor_ret_iva:.2f}"
    if v.valor_ret_renta:
        etree.SubElement(infoFactura, "valorRetRenta").text = f"{v.valor_ret_renta:.2f}"
    detalles = etree.SubElement(root, "detalles")
    for item in v.items:
        d = etree.SubElement(detalles, "detalle")
        if item.codigo:
            etree.SubElement(d, "codigoPrincipal").text = item.codigo[:25]
        if item.codigo_auxiliar:
            etree.SubElement(d, "codigoAuxiliar").text = item.codigo_auxiliar[:25]
        etree.SubElement(d, "descripcion").text = item.descripcion[:300]
        etree.SubElement(d, "cantidad").text = f"{item.cantidad:.2f}"
        etree.SubElement(d, "precioUnitario").text = f"{item.precio_unitario:.2f}"
        etree.SubElement(d, "descuento").text = f"{item.descuento:.2f}"
        precio_total_sin_impuesto = (item.cantidad * item.precio_unitario) - item.descuento
        etree.SubElement(d, "precioTotalSinImpuesto").text = f"{precio_total_sin_impuesto:.2f}"
        impuestos = etree.SubElement(d, "impuestos")
        impuesto_det = etree.SubElement(impuestos, "impuesto")
        etree.SubElement(impuesto_det, "codigo").text = "2"
        etree.SubElement(impuesto_det, "codigoPorcentaje").text = "0"
        etree.SubElement(impuesto_det, "tarifa").text = "0.00"
        etree.SubElement(impuesto_det, "baseImponible").text = f"{precio_total_sin_impuesto:.2f}"
        etree.SubElement(impuesto_det, "valor").text = "0.00"
    infoAdicional = etree.SubElement(root, "infoAdicional")
    if v.cliente.correo:
        etree.SubElement(infoAdicional, "campoAdicional", nombre="Email").text = v.cliente.correo[:300]
    return clave, etree.tostring(root, encoding="UTF-8", xml_declaration=True).decode("utf-8")

# --------------------------- FIRMA --------------------------- #

from datetime import datetime, timezone
import traceback


def validar_certificado(cert_path: str, cert_pass: str) -> bool:
    """Valida que el certificado sea válido y tenga cadena de confianza"""
    try:
        with open(cert_path, "rb") as f:
            p12_data = f.read()
        private_key, cert, additional_certs = pkcs12.load_key_and_certificates(
            p12_data,
            cert_pass.encode() if cert_pass else None,
            default_backend()
        )
        if cert is None:
            logger.error("No se pudo cargar el certificado")
            return False
        if private_key is None:
            logger.error("No se pudo cargar la clave privada")
            return False
        if not additional_certs:
            logger.warning("No se encontraron certificados adicionales en el archivo PKCS12")
        else:
            logger.info(f"Se encontraron {len(additional_certs)} certificados adicionales")
        cert_not_after = cert.not_valid_after
        cert_not_before = cert.not_valid_before
        # Make now comparable to cert datetimes
        if cert_not_after.tzinfo is None:
            now = datetime.utcnow()
        else:
            now = datetime.now(timezone.utc)
        if cert_not_after < now:
            logger.error(f"Certificado expirado el {cert_not_after}")
            return False
        if cert_not_before > now:
            logger.error(f"Certificado aún no válido hasta {cert_not_before}")
            return False
        logger.info(f"Certificado válido para: {cert.subject}")
        logger.info(f"Válido desde: {cert_not_before} hasta: {cert_not_after}")
        return True
    except Exception as e:
        logger.error(f"Error validando certificado: {e}")
        logger.error(traceback.format_exc())
        return False

# Subclase para permitir algoritmos legados (SHA1) si es necesario
class XAdESSignerAllowDeprecated(XAdESSigner):
    def check_deprecated_methods(self):
        # Sobrescribimos la comprobación para permitir algoritmos legacy como SHA1
        # Úselo sólo si su receptor (SRI) exige SHA1. SHA1 está deprecated por motivos de seguridad.
        return None


def firmar_xml(xml_sin_firma: str, cert_path: str, cert_pass: str) -> str:
    """
    Firma un XML para el SRI usando XAdES-BES con signxml (XAdESSigner).
    Esta implementación usa la clave y certificado cargados desde un P12.
    """
    if not cert_path or not cert_pass:
        logger.error("Ruta o contraseña del certificado no proporcionada.")
        raise ValueError("Certificado o contraseña no proporcionados.")
    try:
        with open(cert_path, "rb") as f:
            p12_data = f.read()
        private_key, cert, additional_certs = pkcs12.load_key_and_certificates(
            p12_data,
            cert_pass.encode() if cert_pass else None,
            default_backend()
        )
        if cert is None or private_key is None:
            raise RuntimeError("No se pudo extraer clave/cer del PKCS12")
        # Preparar la cadena de certificados (si existe)
        cert_chain = [cert]
        if additional_certs:
            cert_chain.extend(additional_certs)
        # Configurar el firmador XAdES. Aquí usamos RSA-SHA1 / SHA1 porque muchos sistemas SRI antiguos lo esperan.
        # Si su SRI acepta SHA256, cambie a SignatureMethod.RSA_SHA256 y DigestAlgorithm.SHA256.
        signer = XAdESSignerAllowDeprecated(
            method=methods.enveloped,
            signature_algorithm=SignatureMethod.RSA_SHA1,
            digest_algorithm=DigestAlgorithm.SHA1,
            c14n_algorithm=CanonicalizationMethod.CANONICAL_XML_1_0
        )
        # Parsear el XML (usamos lxml)
        root = etree.fromstring(xml_sin_firma.encode("utf-8"))
        # Asegurarse de que el atributo id (minúscula) es considerado por signxml
        signed_root = signer.sign(
            root,
            key=private_key,
            cert=cert_chain,
            reference_uri="#comprobante",
            id_attribute="id"
        )
        xml_firmado = etree.tostring(signed_root, encoding="UTF-8", xml_declaration=True).decode("utf-8")
        logger.info("XML firmado con éxito utilizando signxml (XAdES-BES).")
        return xml_firmado
    except Exception as e:
        logger.error(f"Error al firmar XML con signxml: {e}")
        logger.error(traceback.format_exc())
        raise


def verificar_estructura_firma(xml_firmado: str):
    try:
        root = etree.fromstring(xml_firmado.encode('utf-8'))
        signature_elements = root.findall('.//{http://www.w3.org/2000/09/xmldsig#}Signature')
        if not signature_elements:
            logger.error("No se encontró elemento Signature en el XML")
            return False
        logger.info(f"Se encontraron {len(signature_elements)} elementos Signature")
        signature = signature_elements[0]
        signed_info = signature.find('{http://www.w3.org/2000/09/xmldsig#}SignedInfo')
        signature_value = signature.find('{http://www.w3.org/2000/09/xmldsig#}SignatureValue')
        key_info = signature.find('{http://www.w3.org/2000/09/xmldsig#}KeyInfo')
        if signed_info is None:
            logger.error("Falta elemento SignedInfo en la firma")
            return False
        if signature_value is None:
            logger.error("Falta elemento SignatureValue en la firma")
            return False
        if key_info is None:
            logger.error("Falta elemento KeyInfo en la firma")
            return False
        logger.info("Estructura de firma XML es válida")
        return True
    except Exception as e:
        logger.error(f"Error verificando estructura de firma: {e}")
        logger.error(traceback.format_exc())
        return False

# --------------------------- VALIDACIÓN XML --------------------------- #

def validar_xml(xml_content: str) -> bool:
    try:
        etree.fromstring(xml_content.encode('utf-8'))
        return True
    except Exception as e:
        logger.error(f"XML inválido: {e}")
        return False

# --------------------------- SOAP --------------------------- #

def crear_cliente(wsdl_url: str) -> Client:
    session = Session()
    session.timeout = SRI_TIMEOUT
    transport = Transport(session=session)
    return Client(wsdl=wsdl_url, transport=transport)

def enviar_factura_sri(xml_firmado: str) -> Tuple[str, str]:
    try:
        client = crear_cliente(WSDL_RECEPCION)
        xml_b64 = base64.b64encode(xml_firmado.encode('utf-8')).decode('utf-8')
        debug_file = os.path.join(DIR_FACTURAS, "debug_last_xml.xml")
        with open(debug_file, "w", encoding="utf-8") as f:
            f.write(xml_firmado)
        resp = client.service.validarComprobante(xml_b64)
        if resp and hasattr(resp, "estado"):
            logger.info(f"Respuesta SRI: {resp.estado}")
            if hasattr(resp, "comprobantes"):
                return resp.estado, str(resp.comprobantes)
            return resp.estado, "Sin mensaje adicional"
        else:
            return "ERROR", "Respuesta inválida del SRI"
    except Fault as e:
        logger.error(f"Fault del SRI: {e}")
        return "ERROR", f"Fault: {str(e)}"
    except Exception as e:
        logger.error(f"Error inesperado: {e}")
        return "ERROR", f"Error inesperado: {str(e)}"

# --------------------------- MAIN --------------------------- #

def main():
    logger.info("Iniciando proceso de EMISIÓN de facturas al SRI...")
    ventas = fetch_ventas_pendientes_envio(LIMITE_ENVIO)
    if not ventas:
        logger.info("No hay ventas pendientes de envío al SRI")
        return
    logger.info(f"Encontradas {len(ventas)} ventas pendientes de envío")
    cn = db_conn()
    try:
        cur = cn.cursor()
        for v in ventas:
            logger.info(f"Procesando venta ID: {v.venta_id}")
            try:
                if not validar_certificado(v.empresa.cert_path, v.empresa.cert_password):
                    logger.error(f"Certificado inválido para venta {v.venta_id}")
                    cur.execute(
                        """
                        UPDATE ventas 
                        SET estado_sri='ERROR', mensaje_sri=%s, 
                            ultimo_intento_envio=NOW(), intentos_envio=intentos_envio + 1 
                        WHERE venta_id=%s
                        """,
                        ("Certificado inválido", v.venta_id)
                    )
                    cn.commit()
                    continue
                clave_acceso, xml_sin_firma = factura_xml(v)
                logger.info(f"Clave de acceso generada: {clave_acceso}")
                xml_firmado = firmar_xml(xml_sin_firma, v.empresa.cert_path, v.empresa.cert_password)
                if not verificar_estructura_firma(xml_firmado):
                    logger.error(f"Problema con la estructura de firma para venta {v.venta_id}")
                    cur.execute(
                        """
                        UPDATE ventas 
                        SET estado_sri='ERROR', mensaje_sri=%s, 
                            ultimo_intento_envio=NOW(), intentos_envio=intentos_envio + 1 
                        WHERE venta_id=%s
                        """,
                        ("Estructura de firma inválida", v.venta_id)
                    )
                    cn.commit()
                    continue
                if not validar_xml(xml_firmado):
                    logger.error(f"XML inválido para venta {v.venta_id}")
                    cur.execute(
                        """
                        UPDATE ventas 
                        SET estado_sri='ERROR', mensaje_sri=%s, 
                            ultimo_intento_envio=NOW(), intentos_envio=intentos_envio + 1 
                        WHERE venta_id=%s
                        """,
                        ("XML inválido", v.venta_id)
                    )
                    cn.commit()
                    continue
                xml_file = os.path.join(DIR_FACTURAS, f"{clave_acceso}.xml")
                with open(xml_file, "w", encoding="utf-8") as f:
                    f.write(xml_firmado)
                estado_sri, mensaje_sri = enviar_factura_sri(xml_firmado)
                logger.info(f"Resultado Recepción SRI: {estado_sri} - {mensaje_sri}")
                cur.execute(
                    """
                    UPDATE ventas 
                    SET estado_sri=%s, mensaje_sri=%s, clave_acceso=%s, 
                        ultimo_intento_envio=NOW(), intentos_envio=intentos_envio + 1 
                    WHERE venta_id=%s
                    """,
                    (estado_sri, str(mensaje_sri)[:255], clave_acceso, v.venta_id)
                )
                cn.commit()
                time.sleep(2)
            except Exception as e:
                logger.error(f"Error procesando venta {v.venta_id}: {e}")
                logger.error(traceback.format_exc())
                cur.execute(
                    """
                    UPDATE ventas 
                    SET estado_sri='ERROR', mensaje_sri=%s, 
                        ultimo_intento_envio=NOW(), intentos_envio=intentos_envio + 1 
                    WHERE venta_id=%s
                    """,
                    (str(e)[:255], v.venta_id)
                )
                cn.commit()
    finally:
        cn.close()
    logger.info("Proceso de EMISIÓN de facturas al SRI completado")

if __name__ == "__main__":
    main()