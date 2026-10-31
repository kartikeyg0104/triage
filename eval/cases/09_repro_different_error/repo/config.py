def load_config(text):
    settings = {}
    for line in text.splitlines():
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip()
    return settings
