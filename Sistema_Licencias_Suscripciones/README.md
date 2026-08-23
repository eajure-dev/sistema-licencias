# Sistema de Licencias y Suscripciones

Primera versión funcional para administrar clientes, planes, pagos, licencias y actualizaciones.

## Inicio rápido

1. Instala Python 3.11 o superior.
2. Abre una terminal dentro de esta carpeta.
3. Ejecuta:

```bash
pip install -r requirements.txt
python app.py
```

4. Abre:

http://127.0.0.1:5000

## Usuario administrador inicial

- Usuario: `admin`
- Contraseña: `admin123`

**Cámbiala antes de usar el sistema en producción.**

## Qué incluye

- Registro de clientes.
- Portal de cliente.
- Planes configurables: precio, duración, equipos y actualizaciones.
- Licencias con códigos únicos.
- Activación vinculada a una huella del equipo.
- Renovación automática de licencias mediante webhook de pago.
- Endpoint para que el POS consulte la licencia.
- Versiones y actualizaciones configurables.
- Correos preparados mediante SMTP.
- SQLite: todos los datos quedan dentro de la carpeta `data/`.
- Subida de instaladores o archivos de actualización.

## Importante sobre pagos

El endpoint `/api/payments/webhook` está preparado como punto de integración.
No debe exponerse públicamente sin verificar la firma oficial del proveedor de pagos.

Para conectarlo a un proveedor real se agregará su verificación de firma y el campo
de referencia de pago correspondiente.

## API principal para el POS

POST `/api/license/check`

Ejemplo:

```json
{
  "code": "POS-XXXX-XXXX-XXXX",
  "machine_id": "identificador-unico-del-equipo",
  "version": "1.0.0"
}
```

POST `/api/license/activate`

Ejemplo:

```json
{
  "code": "POS-XXXX-XXXX-XXXX",
  "machine_id": "identificador-unico-del-equipo"
}
```


## Webpay / Transbank

La integración usa el SDK oficial de Transbank para Python.

En el panel administrador entra a **Correo / Configuración** y completa:

- Ambiente: Integración para pruebas o Producción.
- Código de comercio.
- API Key.
- URL pública del sistema, por ejemplo: `https://tudominio.cl`.

El flujo es:

1. Se crea la orden y el registro queda pendiente.
2. El sistema crea la transacción Webpay.
3. El cliente es enviado al formulario de pago de Transbank.
4. Transbank vuelve a `/webpay/commit`.
5. El servidor ejecuta `commit(token_ws)` contra Transbank.
6. Solo si la respuesta está autorizada y el monto coincide, se activa o renueva la licencia.
7. Se envía el correo con el código e instrucciones.

Nunca pongas la API Key de producción dentro del programa POS del cliente.


## Invitaciones gratuitas y descarga del programa

El administrador puede usar **Invitar gratis** para crear una licencia de prueba por cualquier cantidad de días y enviar automáticamente un correo con el código y el enlace de descarga.

En **Programa y actualizaciones**, sube el archivo ZIP o instalador del POS y marca:

`Usar este archivo como descarga principal del programa`

Los clientes con una licencia activa podrán descargarlo desde su portal.
También se pueden agregar días a cualquier licencia desde su ficha.
