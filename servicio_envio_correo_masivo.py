# servicio_envio_correos.py (versión corregida)
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import os
import mysql.connector as mysql
from datetime import datetime
from fpdf import FPDF
import base64

# Configuración
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("envio_correos")

# Configuración de base de datos
DB_CFG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "cangrivic_user"),
    "password": os.getenv("DB_PASS", "Cangrivic2024!"),
    "database": os.getenv("DB_NAME", "db_factura_marisco")
}

# Configuración de email
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER", "juancarloschoezbanchon@gmail.com")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "fkua iuhh rdyk ynqa")

# Configuración de directorios
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_DIR = os.path.join(BASE_DIR, "archivos_sistema", "pdf_facturas")
LOGS_DIR = os.path.join(BASE_DIR, "archivos_sistema", "logs")
ENVIADOS_DIR = os.path.join(BASE_DIR, "archivos_sistema", "correos", "enviados")
FALLIDOS_DIR = os.path.join(BASE_DIR, "archivos_sistema", "correos", "fallidos")

# Crear directorios si no existen
os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(ENVIADOS_DIR, exist_ok=True)
os.makedirs(FALLIDOS_DIR, exist_ok=True)

def db_conn():
    return mysql.connect(**DB_CFG)

def obtener_ventas_para_envio():
    """Obtiene ventas autorizadas que no han sido enviadas por correo"""
    cn = db_conn()
    try:
        cur = cn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT v.venta_id, v.clave_acceso, v.numero_autorizacion, 
                   v.fecha_autorizacion, c.cliente_correo, c.cliente_nombre,
                   c.cliente_identificacion, c.cliente_direccion, c.cliente_telefono,
                   v.total, v.fecha_venta, v.secuencial, v.tipo_comprobante,
                   v.establecimiento, v.punto_emision,
                   e.empresa_nombre, e.ruc, e.empresa_direccion, e.empresa_telefono,
                   e.empresa_email
            FROM ventas v
            JOIN cliente c ON v.cliente_id = c.cliente_id
            JOIN empresa e ON v.empresa_id = e.empresa_id
            WHERE v.estado_sri = 'AUTORIZADO' 
            AND v.correo_enviado = 0
            AND v.intentos_envio_correo < 3
            ORDER BY v.fecha_autorizacion ASC
            LIMIT 50
            """)
        return cur.fetchall()
    finally:
        cn.close()

def obtener_detalles_venta(venta_id):
    """Obtiene los detalles (items) de una venta específica"""
    cn = db_conn()
    try:
        cur = cn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT vi.cantidad, vi.precio_unitario, vi.subtotal,
                   p.producto_nombre, p.producto_descripcion, p.producto_codigo, p.iva 
            FROM venta_items vi
            JOIN producto p ON vi.producto_id = p.producto_id
            WHERE vi.venta_id = %s
            """, (venta_id,))
        return cur.fetchall()
    finally:
        cn.close()

def formatear_secuencial(secuencial):
    """Formatea el secuencial a 9 dígitos, manejando tanto strings como números"""
    try:
        # Si es numérico, convertirlo a entero y formatear
        if isinstance(secuencial, (int, float)):
            return f"{int(secuencial):09d}"
        # Si es string, intentar convertir a entero
        elif isinstance(secuencial, str) and secuencial.isdigit():
            return f"{int(secuencial):09d}"
        # Si no es numérico, devolver el string original con padding
        else:
            return secuencial.zfill(9)
    except (ValueError, TypeError):
        # En caso de error, devolver el valor original
        return str(secuencial)
    



from fpdf import FPDF
from datetime import datetime
import os


def generar_pdf_factura(venta):
    """Genera un PDF elegante y profesional con los datos reales de la factura"""
    pdf_filename = f"factura_{venta['venta_id']}.pdf"
    pdf_path = os.path.join(PDF_DIR, pdf_filename)

    if os.path.exists(pdf_path):
        return pdf_path

    try:
        detalles = obtener_detalles_venta(venta['venta_id'])
        secuencial_formateado = formatear_secuencial(venta['secuencial'])

        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)

        # --- Colores corporativos ---
        COLOR_PRINCIPAL = (30, 64, 175)   # Azul fuerte
        COLOR_SECUNDARIO = (240, 240, 240)

        # --- Logo + datos empresa ---
        logo_path = os.path.join(BASE_DIR, "archivos_sistema", "logo_empresa.png")
        if os.path.exists(logo_path):
            pdf.image(logo_path, x=10, y=8, w=30)

        pdf.set_font("Helvetica", "B", 12)
        pdf.set_xy(50, 10)
        pdf.set_text_color(*COLOR_PRINCIPAL)
        pdf.cell(0, 6, venta['empresa_nombre'], ln=1)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(0, 0, 0)
        pdf.set_x(50)
        pdf.multi_cell(0, 5, f"RUC: {venta['ruc']}\nDirección: {venta['empresa_direccion']}\n"
                             f"Teléfono: {venta.get('empresa_telefono','')}\nEmail: {venta.get('empresa_email','')}")
        pdf.ln(5)

        # --- Encabezado Factura ---
        pdf.set_fill_color(*COLOR_PRINCIPAL)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 12, "FACTURA ELECTRÓNICA", 0, 1, "C", True)
        pdf.ln(5)

        # --- Cliente ---
        pdf.set_fill_color(*COLOR_SECUNDARIO)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "INFORMACIÓN DEL CLIENTE", 0, 1, "L", True)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, f"Nombre: {venta['cliente_nombre']}", 0, 1)
        pdf.cell(0, 6, f"Identificación: {venta['cliente_identificacion']}", 0, 1)
        if venta.get('cliente_direccion'):
            pdf.cell(0, 6, f"Dirección: {venta['cliente_direccion']}", 0, 1)
        if venta.get('cliente_telefono'):
            pdf.cell(0, 6, f"Teléfono: {venta['cliente_telefono']}", 0, 1)
        pdf.cell(0, 6, f"Email: {venta['cliente_correo']}", 0, 1)
        pdf.ln(5)

        # --- Datos de factura ---
        pdf.set_fill_color(*COLOR_SECUNDARIO)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "INFORMACIÓN DE LA FACTURA", 0, 1, "L", True)
        pdf.set_font("Helvetica", "", 10)

        numero_factura = f"{venta['establecimiento']}-{venta['punto_emision']}-{secuencial_formateado}"
        pdf.cell(95, 6, f"Número de Factura: {numero_factura}", 0, 0)
        pdf.cell(95, 6, f"Fecha Emisión: {venta['fecha_venta']}", 0, 1)

        if venta.get('fecha_autorizacion'):
            pdf.cell(0, 6, f"Fecha Autorización: {venta['fecha_autorizacion']}", 0, 1)

        if venta.get('numero_autorizacion'):
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 6, f"N° Autorización: {venta['numero_autorizacion']}")

        # Clave de acceso
        if venta.get('clave_acceso'):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 6, "Clave de Acceso:", 0, 1)
            pdf.set_font("Courier", "", 8)
            pdf.multi_cell(0, 4, venta['clave_acceso'])

        pdf.ln(5)

        # --- Tabla de productos con IVA ---
        pdf.set_fill_color(*COLOR_PRINCIPAL)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(80, 7, "Descripción", 1, 0, "C", True)
        pdf.cell(20, 7, "Cant.", 1, 0, "C", True)
        pdf.cell(30, 7, "P. Unitario", 1, 0, "C", True)
        pdf.cell(25, 7, "IVA", 1, 0, "C", True)
        pdf.cell(35, 7, "Subtotal", 1, 1, "C", True)

        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(0, 0, 0)

        fill = False
        for detalle in detalles:
            pdf.set_fill_color(245, 245, 245) if fill else pdf.set_fill_color(255, 255, 255)

            descripcion = detalle['producto_nombre']
            if len(descripcion) > 48:
                descripcion = descripcion[:25] + "..."

            cantidad = detalle['cantidad']
            precio_unitario = detalle['precio_unitario']
            iva = detalle.get('iva', 0.0)  # Si no viene, se pone 0.0
            subtotal = detalle['subtotal']

            pdf.cell(80, 7, descripcion, 1, 0, "L", fill)
            pdf.cell(20, 7, str(cantidad), 1, 0, "C", fill)
            pdf.cell(30, 7, f"${precio_unitario:.2f}", 1, 0, "R", fill)
            pdf.cell(25, 7, f"${iva:.2f}", 1, 0, "R", fill)
            pdf.cell(35, 7, f"${subtotal:.2f}", 1, 1, "R", fill)

            fill = not fill

        # --- Totales ---
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_fill_color(230, 230, 230)
        pdf.cell(155, 8, "TOTAL", 1, 0, "R", True)
        pdf.cell(35, 8, f"${venta['total']:.2f}", 1, 1, "R", True)

        pdf.ln(8)

        # --- Información adicional ---
        pdf.set_fill_color(*COLOR_SECUNDARIO)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 8, "INFORMACIÓN ADICIONAL", 0, 1, "L", True)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, "Factura autorizada electrónicamente por el SRI.\n"
                             "Verifique en: https://srienlinea.sri.gob.ec/\n\n"
                             "¡Gracias por su preferencia!")

        # --- Pie de página ---
        pdf.set_y(-25)
        pdf.set_draw_color(200, 200, 200)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 6, f"Documento generado el: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}", 0, 1, "C")
        contacto = []
        if venta.get('empresa_telefono'):
            contacto.append(f"Tel: {venta['empresa_telefono']}")
        if venta.get('empresa_email'):
            contacto.append(f"Email: {venta['empresa_email']}")
        if contacto:
            texto_contacto = " | ".join(contacto)
            pdf.multi_cell(0, 6, texto_contacto, 0, "C")  # centra y ajusta si es muy largo

        pdf.output(pdf_path)
        return pdf_path

    except Exception as e:
        logger.error(f"Error generando PDF para venta {venta['venta_id']}: {e}")
        raise



def enviar_correo(destinatario, nombre, venta, pdf_path):
    """Envía el correo con la factura adjunta"""
    try:
        # Verificar credenciales
        if not EMAIL_USER or not EMAIL_PASSWORD:
            raise ValueError("Credenciales de email no configuradas")
        
        # Formatear secuencial
        secuencial_formateado = formatear_secuencial(venta['secuencial'])
        numero_factura = f"{venta['establecimiento']}-{venta['punto_emision']}-{secuencial_formateado}"
        
        msg = MIMEMultipart()
        msg['From'] = EMAIL_USER
        msg['To'] = destinatario
        msg['Subject'] = f"Factura Electrónica #{numero_factura}"
        
        # Formatear fecha para el cuerpo del correo
        fecha_venta_str = venta['fecha_venta']
        if isinstance(venta['fecha_venta'], datetime):
            fecha_venta_str = venta['fecha_venta'].strftime('%d/%m/%Y')
        elif isinstance(venta['fecha_venta'], str):
            try:
                fecha_obj = datetime.strptime(venta['fecha_venta'], '%Y-%m-%d %H:%M:%S')
                fecha_venta_str = fecha_obj.strftime('%d/%m/%Y')
            except ValueError:
                pass  # Mantener el string original
        
        cuerpo = f"""
        Estimado/a {nombre},
        
        Adjunto encontrará su factura electrónica autorizada.
        
        Número de factura: {numero_factura}
        Fecha de emisión: {fecha_venta_str}
        Total: ${venta['total']:.2f}
        
        Esta factura ha sido autorizada electrónicamente por el SRI.
        Puede verificar su autenticidad en https://srienlinea.sri.gob.ec/
        
        Gracias por su preferencia.
        
        Atentamente,
        {venta['empresa_nombre']}
        """
        msg.attach(MIMEText(cuerpo, 'plain'))
        
        # Adjuntar PDF
        with open(pdf_path, "rb") as f:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="factura_{venta["venta_id"]}.pdf"')
            msg.attach(part)
        
        # Enviar correo
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.ehlo()
        server.starttls()
        server.ehlo()
        
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_USER, destinatario, msg.as_string())
        server.quit()
        
        # Mover PDF a enviados
        if os.path.exists(pdf_path):
            destino = os.path.join(ENVIADOS_DIR, os.path.basename(pdf_path))
            os.rename(pdf_path, destino)
        
        return True, "Correo enviado exitosamente"
        
    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"Error de autenticación SMTP: {e}. Verifica usuario y contraseña de aplicación."
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"Error enviando correo: {str(e)}"
        logger.error(error_msg)
        # Mover PDF a fallidos
        if os.path.exists(pdf_path):
            destino = os.path.join(FALLIDOS_DIR, os.path.basename(pdf_path))
            os.rename(pdf_path, destino)
        return False, error_msg

def actualizar_estado_envio(venta_id, exito, mensaje):
    """Actualiza el estado de envío del correo en la base de datos"""
    cn = db_conn()
    try:
        cur = cn.cursor()
        if exito:
            cur.execute(
                """
                UPDATE ventas 
                SET correo_enviado = TRUE, 
                    fecha_envio_correo = NOW(),
                    mensaje_envio_correo = %s
                WHERE venta_id = %s
                """,
                (mensaje, venta_id)
            )
        else:
            cur.execute(
                """
                UPDATE ventas 
                SET intentos_envio_correo = intentos_envio_correo + 1,
                    mensaje_envio_correo = %s,
                    ultimo_intento_envio_correo = NOW()
                WHERE venta_id = %s
                """,
                (mensaje, venta_id)
            )
        cn.commit()
    finally:
        cn.close()

def main():
    logger.info("Iniciando proceso de envío de correos...")
    
    # Verificar configuración de email
    if not EMAIL_USER or not EMAIL_PASSWORD:
        logger.error("ERROR: Variables de entorno EMAIL_USER y/o EMAIL_PASSWORD no están configuradas")
        return
    
    ventas = obtener_ventas_para_envio()
    
    if not ventas:
        logger.info("No hay ventas pendientes de envío por correo")
        return
    
    logger.info(f"Encontradas {len(ventas)} ventas para enviar por correo")
    
    for venta in ventas:
        try:
            pdf_path = generar_pdf_factura(venta)
            exito, mensaje = enviar_correo(
                venta['cliente_correo'], 
                venta['cliente_nombre'],
                venta,
                pdf_path
            )
            
            actualizar_estado_envio(venta['venta_id'], exito, mensaje)
            
            if exito:
                logger.info(f"Correo enviado exitosamente para venta {venta['venta_id']}")
            else:
                logger.error(f"Error enviando correo para venta {venta['venta_id']}: {mensaje}")
                
        except Exception as e:
            logger.error(f"Error procesando venta {venta['venta_id']}: {e}")
            actualizar_estado_envio(venta['venta_id'], False, str(e))
    
    logger.info("Proceso de envío de correos completado")

if __name__ == "__main__":
    main()