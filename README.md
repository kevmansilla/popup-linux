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

## Actualizar

La app ejecuta directamente desde el repo, no requiere recompilación. Solo cierra y vuelve a abrir la app para aplicar cambios en el código.

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
