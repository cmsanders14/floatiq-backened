"""Read-only connection check. Prints no credentials or account details."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from floatiq_core.alpaca_data import AlpacaMarketDataProvider, AlpacaDataError


def main():
    try:
        provider = AlpacaMarketDataProvider()
        frame = provider.download("AAPL", "7d", "1d")
        if frame.empty:
            print("No bars returned; connection/data availability is not verified.")
            return 1
        print(f"Alpaca read-only data check passed: feed={provider.feed}, bars={len(frame)}, last_bar={frame.index[-1].isoformat()}")
        return 0
    except AlpacaDataError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
