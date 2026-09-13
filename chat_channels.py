"""Start all configured chat channels with one shared identity router."""
import asyncio
import sys

from diary_agent.channels.runtime import run_channels


def main() -> None:
    try:
        asyncio.run(run_channels())
    except KeyboardInterrupt:
        print("\nChat channels stopped")
    except Exception as error:
        # Config errors are written without credential values. Other exceptions
        # may contain authenticated HTTP URLs and must not be printed verbatim.
        detail = str(error) if isinstance(error, (ValueError, NotImplementedError)) else type(error).__name__
        print(f"Chat channels failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
