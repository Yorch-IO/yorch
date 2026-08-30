# Runbook: preproducción del plano de pago

## Contexto

El plano de pago (`../yorch-tauri-backend`, NestJS, Cognito, multi-organización)
y la aplicación de escritorio están construidos y verificados por pruebas. Lo
que no había ocurrido nunca es que **una persona los usara como cliente**: cada
token con el que se comprobó el plano se acuñó con
`aws cognito-idp admin-initiate-auth`, que no pasa ni cerca del inicio de sesión
de la aplicación.

Este documento es el procedimiento para montar esa comprobación en una máquina
de validación, y para dar de alta a cada cliente. No publica una API ni diseña
infraestructura de producción: todo queda en `127.0.0.1`.

**Escribirlo ejecutándolo es la mitad del punto.** Los scripts que conduce ya
existían; lo que no existía era la prueba de que bastan.

### Lo que está medido, no recordado — 2026-08-29

| Qué | Estado |
|---|---|
| Plano gratuito, `127.0.0.1:8787` | 26 rutas, las siete patas de `/health` en verde |
| Plano de pago, `127.0.0.1:8788` | **28 rutas** = las mismas 26 + `GET /auth/config` + `POST /uploads` |
| Grupo de Cognito | **aplicado** — `yorch-brain-auth.auth.us-west-2.amazoncognito.com`, grupo `us-west-2_69PaEg3l8` |
| Esquema del catálogo | `20260826180000_tenant_required` — sin `DEFAULT` en ninguna columna `tenant_id` |
| Organizaciones | `legacy` (72 documentos) y `acme` (1) |
| Inicio de sesión desde el navegador | **nunca ejecutado** |

## 1. La pila

Los tres `-f` importan y ninguno es opcional.

```bash
cd infra
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml \
  -f docker-compose.adc.yaml --profile paid up -d --build api worker backend
```

- Sin `docker-compose.dev.yaml` el `--build` es un **no-op silencioso**: el
  fichero base nombra un `image:` y la sección `build:` vive sólo en el overlay.
  Compose recrea los contenedores, informa de éxito y deja el código viejo
  sirviendo.
- Sin `docker-compose.adc.yaml` cualquier cosa que llegue a Vertex falla con
  `provider_unavailable` / `DefaultCredentialsError` **mientras `/health` sigue
  mostrando la fila del proveedor en verde**: esa fila es gratis y sólo dice si
  hay un identificador de proyecto puesto. `POST /provider/probe` es lo que
  gasta y por tanto lo que sabe.
- Sin `--profile paid` el servicio `backend` no arranca. Una pila gratuita y
  autogestionada nunca lo levanta, que es para lo que existe el perfil.

**Comprueba lo que el contenedor tiene, no lo que dice el checkout.** Una
constante corregida en el código no cambia nada en una compuerta real hasta que
la imagen se reconstruye:

```bash
docker exec company-brain-api-1 python -c \
  "from brainworker.activities import ingest; print(ingest.OUTPUT_SPREAD)"
```

### La página de OpenAPI está apagada, y encenderla es un acto

`BRAIN_DOCS` vive ahora en el fichero base como `${BRAIN_DOCS:-}`. Vacío es sin
poner, y `env.ts` cae a `off`. Estaba forzado a `"on"` en el overlay de
desarrollo — el mismo que aplica esta pila — así que `/docs` respondía 200 sin
autenticar a quien encontrase el puerto, enumerando cada ruta, cada campo y cada
`kind` de error de una API multi-organización.

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/docs   # 404
```

Para leerla en una máquina de desarrollo: `BRAIN_DOCS=on` en el entorno o en
`infra/.env`, y recrear el servicio. Nunca en la máquina de validación.

## 2. Cognito

Los cuatro valores viven en `infra/cognito.env`, **no en `infra/.env`**: la
aplicación de escritorio reescribe ese fichero entero en cada arranque
(`app/src-tauri/src/stack.rs` construye el cuerpo desde cero), así que lo que se
añada allí sobrevive hasta que alguien abra la aplicación.

```bash
cp infra/cognito.env.example infra/cognito.env
cd ../yorch-aws-platform
terraform -chdir=envs/prod output -raw brain_cognito_user_pool_id
terraform -chdir=envs/prod output -raw brain_cognito_client_id
terraform -chdir=envs/prod output -raw brain_cognito_hosted_ui_domain
```

**Fíjate en el prefijo `brain_`.** Las salidas sin prefijo son las del panel de
noticias: `yorch-prod-news-admins`, con su propia política de contraseñas y su
propia lista de usuarios. Usarlas convertiría a cada cliente de Company Brain en
administrador del sitio de marketing.

No son credenciales. Un identificador de grupo y uno de cliente identifican un
grupo y no autorizan nada; el grupo no tiene secreto de cliente, porque la
aplicación de escritorio es un cliente público que usa PKCE.

El fichero está declarado `required: false`, así que una pila gratuita no lo
necesita. Un arranque de pago sin él **se niega a arrancar nombrando las
variables que le faltan**, que es mejor fallo que un 401 con pinta de problema
de firma:

```bash
docker compose ... up -d backend     # sin cognito.env
docker logs company-brain-backend-1  # "refusing to start: unset required configuration …"
```

## 3. Dar de alta un cliente

Dos directorios tienen que coincidir y ninguno hace la mitad del otro. Cognito
responde «este token es genuino y pertenece al sujeto X»; el catálogo responde
«X puede actuar en la organización Y».

```bash
cd ../yorch-tauri-backend
./scripts/seed-tenant.sh mizpa "Iglesia Mizpa" lib_mizpa "Biblioteca Mizpa"
./scripts/seed-user.sh pastor@mizpa.test mizpa
```

**El slug es la identidad y hay que fijarlo antes de indexar nada.** Renombrar
una organización es un `UPDATE`; re-sluguearla cambia su identificador, y ese
identificador está salado dentro de cada `ver_` y cada `con_` que la
organización produzca. No hay migración para eso que no sea re-proyectar y
volver a pagar los embeddings.

`seed-user.sh` deja `app_user.cognito_sub` a NULL a propósito: **lo reclama la
primera petición autenticada**, venga del navegador o de un `curl` con un token
acuñado a mano. Medido el 2026-08-30: un solo `GET /health` con token dejó
puestos `cognito_sub` y `last_login_at`. Eso mantiene un solo camino de código
para un usuario sembrado y para uno que un operador añada más tarde desde una
dirección sola. Compruébalo:

```sql
SELECT email, cognito_sub IS NOT NULL AS enlazado, last_login_at FROM app_user;
```

Para una persona real, quita `--message-action SUPPRESS` y **no** pases
`--password`: Cognito manda la contraseña temporal y obliga a cambiarla, de modo
que el valor no toca esta máquina.

### Una sola organización por cuenta, en esta versión

El servidor resuelve una membresía única a partir del token y la aplicación no
ofrece selector. Una cuenta con varias membresías recibe `400 tenant_required` y
no tiene manera de resolverlo desde la interfaz — la protección del servidor se
queda como está, pero **no aprovisiones a nadie con más de una**.

## 4. La comprobación de extremo a extremo

En orden, porque cada paso depende del anterior.

1. **Modo nube** en la pantalla de Servicios: `http://127.0.0.1:8788`, Guardar.
   Guardar apuntando a otro sitio **cierra la sesión** — un bearer pertenece al
   servicio que lo emitió.
2. **Iniciar sesión.** Abre el navegador del sistema contra el hosted UI y
   escucha en `127.0.0.1:8789`. Después:
   - la sesión está en el llavero (la pantalla dice cuál de los dos almacenes
     respondió),
   - no queda ningún `session.json`,
   - `app_user.cognito_sub` quedó enlazado por esta entrada.
3. **Subir, aprobar, indexar** un documento pequeño. Vigila la factura contra el
   rango que citó la compuerta: **el rango tiene que sobre-informar**, que es la
   única dirección en la que se le permite fallar.
4. **Preguntar** y comprobar que las citas llevan localizadores byte-exactos.
5. **Cerrar sesión** y confirmar que una petición sin sesión se rechaza y que la
   aplicación muestra `error.notSignedIn` y no un mensaje crudo.
6. **Aislamiento, en las dos direcciones.** Cuenta filas, nodos y puntos por
   organización antes y después: `legacy` y `acme` intactas, la nueva sólo con
   lo suyo, y **ninguna fila, nodo ni punto sin organización** en ninguno de los
   tres almacenes. Después, a mano: coge un identificador de ejecución de una
   organización y pide su compuerta como otra. Tiene que responder **404**, no
   403 — un 403 confirmaría que ese identificador existe.
7. **El modo local no se ha movido.** Vuelve a él y comprueba que no se envía
   ninguna cabecera `Authorization`, que no se llama a Cognito, y que una
   importación por el camino de staging local sigue funcionando.

### `POST /uploads` rechaza la organización heredada, y es correcto

Su raíz de espacio de trabajo *es* el volumen, sobre el que el montaje de este
plano es de sólo lectura a propósito: el corpus que precede a la multi-tenancia
lo gestiona el plano local. Escribir en `tenants/<legacy>/inbox` «funcionaba» y
producía una ruta que `stage_source` luego rechazaba — un callejón sin salida que
devolvía 200. Ahora responde `409 tenant_scope_pending`.

Consecuencia práctica: **la comprobación de extremo a extremo no puede hacerse
con una cuenta de `legacy`.** Por eso el paso 3 usa una organización nueva.

## 5. Dos tropiezos que cuestan una tarde

**El puerto de Bolt sale de `docker`, no de `infra/.env`.** Las suites de grafo
y de catálogo son pruebas de integración: se saltan, nombrando la URL que
intentaron, cuando Memgraph o Postgres no están. Con la pila arriba:

```bash
cd worker && BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7789 uv run pytest -q
```

Sin esa variable el conftest prueba 7788 — que es lo que pide `Ports::default()`
— y toda la suite de grafo se salta en silencio. Es exactamente la forma de una
suite que parece verde porque no se ejecutó.

**`infra/.env` no sabe nada del plano de pago.** `BRAIN_BACKEND_IMAGE`,
`BRAIN_BACKEND_HOST_PORT`, `BRAIN_BACKEND_CONTEXT` y `BRAIN_COGNITO_ENV_FILE`
caen a los valores por defecto de compose, porque `stack.rs` reescribe ese
fichero entero en cada arranque de la aplicación. Para esta pila está bien —
los defaults son los correctos — pero no pongas nada ahí esperando que dure.

## 6. Lo que este entorno **no** valida

Dicho explícitamente, porque un entorno que casi produce es el que se confunde
con producción:

- No hay acceso remoto de escritorio. Todo está atado a `127.0.0.1`.
- No hay HTTPS público, ni certificado, ni nombre de dominio.
- No hay alta de usuarios por sí mismos: dar de alta es el paso 3 y es manual.
- No es operación en producción: no hay copias de seguridad programadas, ni
  alertas, ni rotación de nada.
- El plano gratuito sigue sin autenticación, y es seguro sólo porque nada fuera
  de la máquina puede enrutar hasta él.
