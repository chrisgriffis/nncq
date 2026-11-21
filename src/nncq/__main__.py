if __name__ == "__main__":   # guard: multiprocessing (spawn) re-imports this module in workers
    from nncq.cli import main
    raise SystemExit(main())
