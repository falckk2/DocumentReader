from src.app import DocumentReaderApp


def main():
    app = DocumentReaderApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
