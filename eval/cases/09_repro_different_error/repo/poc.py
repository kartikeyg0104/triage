from config import load_config

print(load_config("name = demo\n\nport = 8080")["port"])
