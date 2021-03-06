#!/bin/bash 

VENV_LOCATION='./venv';

if ! [[ -d $VENV_LOCATION ]]; then
    echo "Virtual environment not present. Creating virtual environment named 'venv' at $VENV_LOCATION.";
    python3 -m venv $VENV_LOCATION;
fi

source ./venv/bin/activate

docker-compose build
docker-compose up -d