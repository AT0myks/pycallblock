import wave


def wav_duration(wav):
    """Return WAV file duration in seconds."""
    try:
        with wave.open(str(wav), "rb") as f:
            framerate, nframes = f.getparams()[2:4]
    except (FileNotFoundError, wave.Error):
        return None
    else:
        return nframes / framerate


def set_up_db(connection):
    cur = connection.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS "call_log" (
            "timestamp"    INTEGER NOT NULL,
            "number"       TEXT NOT NULL,
            "name"         TEXT,
            "blocked"      INTEGER NOT NULL,
            PRIMARY KEY("timestamp")
        );""")
    cur.close()
    connection.commit()
