# popup-linux

## Iniciar

```
python3 -m venv env
source env/bin/activate
pip3 install -r requirements.txt
```

## Clear

```
pip freeze > requirements.txt
pip uninstall -r requirements.txt -y
deactivate
rm -r env/
rm -rf \__pycache\__
```
