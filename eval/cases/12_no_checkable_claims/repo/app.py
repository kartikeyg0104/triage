import time


def main():
    started = time.monotonic()
    print("ready in", round(time.monotonic() - started, 3), "s")


if __name__ == "__main__":
    main()
