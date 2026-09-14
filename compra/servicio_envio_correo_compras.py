#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, smtplib, logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from fpdf import FPDF
from comun_compras import db_conn

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("correo_compras")

SMTP_SERVER    = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
EMAIL_USER     = os.getenv("EMAIL_USER", "juancarloschoezbanchon@gmail.com")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "fkua iuhh rdyk ynqa")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_DIR  = os.path.join(BASE_DIR, "archivos_sistema", "pdf_compras")
os.makedirs(PDF_DIR, exist_ok=True)


def fetch_compras_para_envio():
    cn = db_conn()
    try:
        cur = cn.cursor(dictionary=True)
        cur.execute("""
            SELECT c.compra_id, c.clave_acceso, c.numero_autorizacion,
                   c.fecha_autorizacion, c.total, c.fecha_compra,
                   c.secuencial, c.establecimiento, c.punto_emision,
                   c.clave_acceso_retencion, c.numero_autorizacion_retencion,
                   c.fecha_autorizacion_retencion, c.secuencial_retencion,
                   p.proveedor_correo, p.proveedor_nombre,
                   p.proveedor_identificacion, p.proveedor_direccion,
                   p.proveedor_telefono,
                   e.empresa_nombre, e.ruc, e.empresa_direccion,
                   e.empresa_telefono, e.empresa_email
            FROM compras c
            JOIN proveedor p ON p.proveedor_id = c.proveedor_id
            JOIN empresa   e ON e.empresa_id   = c.empresa_id
            WHERE c.estado_sri = 'AUTORIZADO'
              AND c.estado_sri_retencion = 'AUTORIZADO'
              AND c.correo_enviado_compra = 0
              AND COALESCE(c.intentos_envio_correo_compra,0) < 3
            ORDER BY c.fecha_autorizacion_retencion ASC
            LIMIT 50
        """)
        return cur.fetchall()
    finally:
        cn.close()


def fetch_items(compra_id):
    cn = db_conn()
    try:
        cur = cn.cursor(dictionary=True)
        cur.execute("""
            SELECT ci.cantidad, ci.precio_unitario, ci.subtotal,
                   p.producto_nombre, p.producto_codigo
            FROM compra_items ci
            JOIN producto p ON p.producto_id = ci.producto_id
            WHERE ci.compra_id=%s
        """, (compra_id,))
        return cur.fetchall()
    finally:
        cn.close()


def _fmt_sec(s):
    try: return f"{int(s):09d}"
    except Exception: return str(s)


def generar_pdf_liquidacion(c):
    path = os.path.join(PDF_DIR, f"liquidacion_{c['compra_id']}.pdf")
    if os.path.exists(path): return path
    items = fetch_items(c["compra_id"])
    pdf = FPDF(); pdf.add_page(); pdf.set_auto_page_break(True, 15)

    COLOR = (30, 64, 175)
    pdf.set_fill_color(*COLOR); pdf.set_text_color(255,255,255)
    pdf.set_font("Helvetica","B",14)
    pdf.cell(0,10,"LIQUIDACIÓN DE COMPRA",0,1,"C",True); pdf.ln(3)

    pdf.set_text_color(0,0,0); pdf.set_font("Helvetica","B",11)
    pdf.cell(0,6,c["empresa_nombre"],0,1)
    pdf.set_font("Helvetica","",9)
    pdf.multi_cell(0,5,f"RUC: {c['ruc']}\nDirección: {c['empresa_direccion']}\n"
                       f"Tel: {c.get('empresa_telefono','')}  Email: {c.get('empresa_email','')}")
    pdf.ln(2)
    pdf.set_font("Helvetica","B",10); pdf.cell(0,6,"PROVEEDOR",0,1)
    pdf.set_font("Helvetica","",9)
    pdf.multi_cell(0,5,f"{c['proveedor_nombre']}\nID: {c['proveedor_identificacion']}\n"
                       f"Dirección: {c['proveedor_direccion'] or ''}\n"
                       f"Tel: {c['proveedor_telefono'] or ''}  Email: {c['proveedor_correo']}")
    pdf.ln(2)
    num = f"{c['establecimiento']}-{c['punto_emision']}-{_fmt_sec(c['secuencial'])}"
    pdf.set_font("Helvetica","",9)
    pdf.cell(0,5,f"N° Liquidación: {num}",0,1)
    pdf.cell(0,5,f"Fecha: {c['fecha_compra']}",0,1)
    if c.get("numero_autorizacion"):
        pdf.multi_cell(0,5,f"N° Autorización: {c['numero_autorizacion']}")
    pdf.ln(3)

    pdf.set_fill_color(*COLOR); pdf.set_text_color(255,255,255)
    pdf.set_font("Helvetica","B",9)
    pdf.cell(80,7,"Descripción",1,0,"C",True)
    pdf.cell(20,7,"Cant.",1,0,"C",True)
    pdf.cell(30,7,"P. Unit.",1,0,"C",True)
    pdf.cell(35,7,"Subtotal",1,1,"C",True)
    pdf.set_text_color(0,0,0); pdf.set_font("Helvetica","",9)
    for it in items:
        pdf.cell(80,6,str(it["producto_nombre"])[:48],1,0)
        pdf.cell(20,6,str(it["cantidad"]),1,0,"C")
        pdf.cell(30,6,f"${it['precio_unitario']:.2f}",1,0,"R")
        pdf.cell(35,6,f"${it['subtotal']:.2f}",1,1,"R")
    pdf.set_font("Helvetica","B",10)
    pdf.cell(130,7,"TOTAL",1,0,"R",True)
    pdf.cell(35,7,f"${c['total']:.2f}",1,1,"R",True)
    pdf.output(path)
    return path


def generar_pdf_retencion(c):
    path = os.path.join(PDF_DIR, f"retencion_{c['compra_id']}.pdf")
    if os.path.exists(path): return path
    pdf = FPDF(); pdf.add_page()
    COLOR = (30, 64, 175)
    pdf.set_fill_color(*COLOR); pdf.set_text_color(255,255,255)
    pdf.set_font("Helvetica","B",14)
    pdf.cell(0,10,"COMPROBANTE DE RETENCIÓN",0,1,"C",True); pdf.ln(3)
    pdf.set_text_color(0,0,0); pdf.set_font("Helvetica","B",11)
    pdf.cell(0,6,c["empresa_nombre"],0,1)
    pdf.set_font("Helvetica","",9)
    pdf.multi_cell(0,5,f"RUC: {c['ruc']}\nDirección: {c['empresa_direccion']}")
    pdf.ln(2)
    pdf.set_font("Helvetica","B",10); pdf.cell(0,6,"SUJETO RETENIDO",0,1)
    pdf.set_font("Helvetica","",9)
    pdf.multi_cell(0,5,f"{c['proveedor_nombre']}\nID: {c['proveedor_identificacion']}")
    pdf.ln(2)
    num = f"{c['establecimiento']}-{c['punto_emision']}-{_fmt_sec(c['secuencial_retencion'])}"
    pdf.set_font("Helvetica","",9)
    pdf.cell(0,5,f"N° Retención: {num}",0,1)
    if c.get("numero_autorizacion_retencion"):
        pdf.multi_cell(0,5,f"N° Autorización: {c['numero_autorizacion_retencion']}")
    pdf.ln(3)
    base = float(c["total"]); ret = round(base * 0.01, 2)
    pdf.set_fill_color(*COLOR); pdf.set_text_color(255,255,255)
    pdf.set_font("Helvetica","B",9)
    pdf.cell(30,7,"Código",1,0,"C",True)
    pdf.cell(30,7,"Base",1,0,"C",True)
    pdf.cell(25,7,"%",1,0,"C",True)
    pdf.cell(35,7,"Valor Ret.",1,1,"C",True)
    pdf.set_text_color(0,0,0); pdf.set_font("Helvetica","",9)
    pdf.cell(30,7,"312A",1,0,"C")
    pdf.cell(30,7,f"${base:.2f}",1,0,"R")
    pdf.cell(25,7,"1.0",1,0,"C")
    pdf.cell(35,7,f"${ret:.2f}",1,1,"R")
    pdf.output(path)
    return path


def enviar_correo(c, adjuntos):
    msg = MIMEMultipart()
    msg["From"] = EMAIL_USER
    msg["To"] = c["proveedor_correo"]
    msg["Subject"] = f"Liquidación de Compra y Retención N° {_fmt_sec(c['secuencial'])}"
    cuerpo = f"""Estimado/a {c['proveedor_nombre']},

Adjunto encontrará su Liquidación de Compra y su respectivo Comprobante de Retención, ambos autorizados electrónicamente por el SRI.

N° Liquidación: {c['establecimiento']}-{c['punto_emision']}-{_fmt_sec(c['secuencial'])}
Total: ${c['total']:.2f}

N° Retención: {c['establecimiento']}-{c['punto_emision']}-{_fmt_sec(c['secuencial_retencion'])}

Verifique en https://srienlinea.sri.gob.ec/

Atentamente,
{c['empresa_nombre']}
"""
    msg.attach(MIMEText(cuerpo, "plain"))
    for p in adjuntos:
        with open(p, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read()); encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f'attachment; filename="{os.path.basename(p)}"')
            msg.attach(part)
    s = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
    s.ehlo(); s.starttls(); s.ehlo()
    s.login(EMAIL_USER, EMAIL_PASSWORD)
    s.sendmail(EMAIL_USER, c["proveedor_correo"], msg.as_string())
    s.quit()


def actualizar(compra_id, ok, mensaje):
    cn = db_conn()
    try:
        cur = cn.cursor()
        if ok:
            cur.execute("""
                UPDATE compras SET correo_enviado_compra=TRUE,
                    fecha_envio_correo_compra=NOW(),
                    mensaje_envio_correo_compra=%s
                WHERE compra_id=%s
            """, (mensaje, compra_id))
        else:
            cur.execute("""
                UPDATE compras
                SET intentos_envio_correo_compra=COALESCE(intentos_envio_correo_compra,0)+1,
                    mensaje_envio_correo_compra=%s,
                    ultimo_intento_envio_correo_compra=NOW()
                WHERE compra_id=%s
            """, (mensaje, compra_id))
        cn.commit()
    finally:
        cn.close()


def main():
    logger.info("Enviando correos de compras (liquidación + retención)...")
    compras = fetch_compras_para_envio()
    if not compras:
        logger.info("Sin compras pendientes de correo"); return
    for c in compras:
        try:
            p_liq = generar_pdf_liquidacion(c)
            p_ret = generar_pdf_retencion(c)
            enviar_correo(c, [p_liq, p_ret])
            actualizar(c["compra_id"], True, "Correo enviado con 2 adjuntos")
            logger.info(f"Correo enviado a {c['proveedor_correo']}")
        except Exception as e:
            logger.error(f"Error compra {c['compra_id']}: {e}")
            actualizar(c["compra_id"], False, str(e)[:500])


if __name__ == "__main__":
    main()