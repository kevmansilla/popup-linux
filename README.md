# popup-linux

## Iniciar
python3 -m venv env
source env/bin/activate
pip3 install -r requirements.txt


## Clear
pip freeze > requirements.txt
pip uninstall -r requirements.txt -y
deactivate
rm -r env/
rm -rf \__pycache\__

## Pre-commit pep8 check
echo -e '#!/bin/sh\nflake8 . --exclude .git,__pycache__,env --ignore=F403,F405' > .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
pip3 install flake8