from ferc_filter.client import FERCClient


def main() -> None:
    client = FERCClient()
    assets = client.data_assets().get("data-assets", [])
    print(f"Found {len(assets)} data assets")
    for asset in assets:
        print(f"\nASSET: {asset.get('title')}")
        print(f"  URL: {asset.get('url')}")
        for dataset in asset.get("data-sets", []):
            print(f"  DATASET {dataset.get('id')}: {dataset.get('title')}")
            print(f"    URL: {dataset.get('url')}")


if __name__ == "__main__":
    main()
