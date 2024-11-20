import os
import subprocess
import threading
import time
import webbrowser
from flask import Flask, send_from_directory
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget, QLineEdit, QPushButton, QLabel, QMessageBox
from werkzeug.serving import make_server

app = Flask(__name__)
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


class FlaskServerThread:
    def __init__(self, app, port=5000):
        self.server = make_server("127.0.0.1", port, app)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join()


class Aria2TorrentStreamer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Aria2 Magnet Link Streamer")
        self.setGeometry(100, 100, 600, 400)

        # GUI layout
        self.layout = QVBoxLayout()
        self.label = QLabel("Enter Magnet Link:")
        self.layout.addWidget(self.label)

        self.magnet_input = QLineEdit(self)
        self.layout.addWidget(self.magnet_input)

        self.start_button = QPushButton("Start Streaming")
        self.start_button.clicked.connect(self.start_streaming)
        self.layout.addWidget(self.start_button)

        self.status_label = QLabel("Status: Waiting for user input.")
        self.layout.addWidget(self.status_label)

        self.open_file_button = QPushButton("Open Downloaded File")
        self.open_file_button.clicked.connect(self.open_downloaded_file)
        self.open_file_button.setEnabled(False)
        self.layout.addWidget(self.open_file_button)

        self.open_location_button = QPushButton("Open File Location")
        self.open_location_button.clicked.connect(self.open_file_location)
        self.open_location_button.setEnabled(False)
        self.layout.addWidget(self.open_location_button)

        self.delete_file_button = QPushButton("Permanently Delete Downloaded File")
        self.delete_file_button.clicked.connect(self.delete_downloaded_file)
        self.delete_file_button.setEnabled(False)
        self.layout.addWidget(self.delete_file_button)

        self.copy_link_button = QPushButton("Copy Stream Link")
        self.copy_link_button.clicked.connect(self.copy_stream_link)
        self.copy_link_button.setEnabled(False)
        self.layout.addWidget(self.copy_link_button)

        self.stop_streaming_button = QPushButton("Stop Streaming")
        self.stop_streaming_button.clicked.connect(self.stop_streaming)
        self.stop_streaming_button.setEnabled(False)
        self.layout.addWidget(self.stop_streaming_button)

        # Set central widget
        central_widget = QWidget()
        central_widget.setLayout(self.layout)
        self.setCentralWidget(central_widget)

        self.server_thread = None
        self.video_file = None

    def start_streaming(self):
        magnet_link = self.magnet_input.text()
        if not magnet_link:
            self.status_label.setText("Status: Please enter a valid magnet link.")
            return

        self.status_label.setText("Status: Starting aria2 download...")
        threading.Thread(target=self.start_aria2_download, args=(magnet_link,)).start()

    def start_aria2_download(self, magnet_link):
        cmd = [
            "aria2c",
            magnet_link,
            "--dir", DOWNLOAD_DIR,
            "--seed-time=0",
            "--file-allocation=none",
        ]
        subprocess.run(cmd)

        # After download, find the video file
        self.video_file = self.find_video_file()
        if self.video_file:
            self.status_label.setText(f"Status: Download complete. Streaming {self.video_file}.")
            self.open_file_button.setEnabled(True)
            self.open_location_button.setEnabled(True)
            self.delete_file_button.setEnabled(True)
            self.stop_streaming_button.setEnabled(True)
            self.copy_link_button.setEnabled(True)
            self.start_flask_server()
        else:
            self.status_label.setText("Status: No video file found in torrent.")

    def find_video_file(self):
        for root, _, files in os.walk(DOWNLOAD_DIR):
            for file in files:
                if file.endswith((".mp4", ".mkv", ".avi", ".mov")):
                    return file
        return None

    def start_flask_server(self):
        if self.server_thread is not None:
            return

        self.server_thread = FlaskServerThread(app)
        self.server_thread.start()
        self.status_label.setText(f"Streaming started: http://127.0.0.1:5000/stream/{self.video_file}")

    def stop_streaming(self):
        if self.server_thread:
            self.server_thread.stop()
            self.server_thread = None
            self.status_label.setText("Streaming stopped.")
            self.stop_streaming_button.setEnabled(False)
            self.copy_link_button.setEnabled(False)

    def open_downloaded_file(self):
        if self.video_file:
            video_path = os.path.join(DOWNLOAD_DIR, self.video_file)
            os.startfile(video_path)  # Windows-specific function to open files with the default app

    def open_file_location(self):
        if self.video_file:
            folder_path = os.path.abspath(DOWNLOAD_DIR)
            webbrowser.open(folder_path)

    def delete_downloaded_file(self):
        if self.video_file:
            if self.server_thread:
                self.stop_streaming()
            video_path = os.path.join(DOWNLOAD_DIR, self.video_file)
            try:
                os.remove(video_path)
                self.status_label.setText("File deleted successfully.")
                self.video_file = None
                self.open_file_button.setEnabled(False)
                self.open_location_button.setEnabled(False)
                self.delete_file_button.setEnabled(False)
                self.stop_streaming_button.setEnabled(False)
                self.copy_link_button.setEnabled(False)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not delete file: {e}")

    def copy_stream_link(self):
        if self.video_file:
            stream_link = f"http://127.0.0.1:5000/stream/{self.video_file}"
            QApplication.clipboard().setText(stream_link)
            QMessageBox.information(self, "Stream Link Copied", "The stream link has been copied to your clipboard.")


@app.route("/stream/<path:filename>")
def stream_video(filename):
    """Serve video file for streaming."""
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=False)


def main():
    app_qt = QApplication([])
    window = Aria2TorrentStreamer()
    window.show()
    app_qt.exec_()


if __name__ == "__main__":
    main()
