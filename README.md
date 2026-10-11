# Self-Healing Operating System

OS course project: a user-space "healer" daemon that detects crashes and hangs,
restarts services, recovers corrupted/deleted files, and rolls back service
state, with a Django dashboard for demo.

## Architecture

    [Demo services] <-- supervised by -- [Healer daemon] -- writes --> [SQLite event log]
     (services/)       (healer/)                                            |
                                                                            v
                                                      [Django dashboard + chaos controls]

- The healer is a **separate daemon**. Django only reads state/logs and sends commands.
- Shared contract: the SQLite event log (`healer/eventlog.py`), WAL mode.

## Setup (Linux / Lubuntu VM)

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python3 -m unittest discover -v

## Run

    python3 -m healer            # start the healer + all services (Ctrl+C to stop)
    python3 -m healer.status     # show service state and recent events
    python3 -m healer.files      # list protected files and versions
    python3 -m healer.files approve data/critical/app.conf --from new.conf
    python3 -m healer.files restore data/critical/app.conf --version 1

    cd web && python3 manage.py runserver    # dashboard at http://localhost:8000 (local only, no login)
    cd web && python3 manage.py test         # dashboard tests