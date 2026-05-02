# popup-linux

Popup de acciones rápidas (copiar, buscar en Google) que aparece al seleccionar texto en cualquier ventana. Funciona en Wayland (KDE) y X11.

## Dependencias del sistema

```bash
# Wayland (KDE Plasma)
sudo pacman -S wl-clipboard

# X11
sudo pacman -S xclip
```

## Instalación

```bash
python3 -m venv env
source env/bin/activate
pip3 install -r requirements.txt
```

## Ejecutar desde terminal

```bash
./popup-linux.sh
```

## Instalar como app de escritorio

El archivo `.desktop` permite lanzar la app desde el menú de aplicaciones sin usar la terminal:

```bash
cp popup-linux.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/
```

Luego busca **"Popup Linux"** en el menú de aplicaciones.

### Inicio automático

Para que se ejecute al iniciar sesión:

```bash
cp popup-linux.desktop ~/.config/autostart/
```

## Correr como servicio (systemd user)

Recomendado si querés que la app se reinicie sola cuando se cuelga o falla (por ejemplo si se desconecta el mouse y el hilo de evdev se cae). Es más robusto que `autostart`.

### Crear el servicio

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/popup-linux.service <<EOF
[Unit]
Description=Popup Linux - acciones al seleccionar texto
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=$HOME/popup-linux/popup-linux.sh
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF
```

> Si clonaste el repo en otra ruta, ajustá `ExecStart` para que apunte al `popup-linux.sh` correcto.

### Habilitar e iniciar

```bash
systemctl --user daemon-reload
systemctl --user enable popup-linux.service     # arranca al iniciar sesión
systemctl --user start popup-linux.service      # arrancar ahora
```

### Comandos útiles

```bash
systemctl --user restart popup-linux            # reiniciar (despues de cambios en el codigo)
systemctl --user stop popup-linux               # parar
systemctl --user status popup-linux             # ver si esta corriendo
journalctl --user -u popup-linux -f             # ver logs en vivo
journalctl --user -u popup-linux -n 100         # ultimas 100 lineas de log
```

### Deshabilitar

```bash
systemctl --user disable --now popup-linux
rm ~/.config/systemd/user/popup-linux.service
```

## Actualizar

La app ejecuta directamente desde el repo, no requiere recompilación. Solo cierra y vuelve a abrir la app para aplicar cambios en el código.

Si la corrés como servicio systemd, alcanza con:

```bash
systemctl --user restart popup-linux
```

Si se agregan nuevas dependencias:

```bash
./env/bin/pip install -r requirements.txt
```

## Limpiar entorno

```bash
pip freeze > requirements.txt
pip uninstall -r requirements.txt -y
deactivate
rm -r env/
rm -rf __pycache__
```
