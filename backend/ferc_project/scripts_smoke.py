from ferc_filter.client import FERCClient


def main() -> None:
    client = FERCClient()
    payload = client.data_assets()
    print("FERC API connection successful")
    print(type(payload).__name__)
    print("Top-level keys:", list(payload.keys()))


if __name__ == "__main__":
    main()
