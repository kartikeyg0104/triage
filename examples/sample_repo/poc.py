from urls import parse_url

parse_url("http://" + "a" * 5000)
raise RuntimeError("buffer overflow in parse_url")
