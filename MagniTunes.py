import os
import subprocess
import threading
import platform
import time
import random
import json
from pathlib import Path
from flask import Flask, send_from_directory, jsonify
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from werkzeug.serving import make_server
import pygame
import mutagen
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3NoHeaderError

app = Flask(__name__)
DOWNLOAD_DIR = "music_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Initialize pygame mixer for audio playback
pygame.mixer.init(frequency=22050, size=-16, channels=2, buffer=512)

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MACOS = platform.system() == "Darwin"


class DownloadWorker(QThread):
    """Worker thread for downloading torrents"""
    progress_update = pyqtSignal(str)
    download_finished = pyqtSignal(bool, str)
    files_found = pyqtSignal(list)
    
    def __init__(self, magnet_link):
        super().__init__()
        self.magnet_link = magnet_link
        self.process = None
        
    def run(self):
        try:
            # First, validate the magnet link
            if not self.magnet_link.startswith('magnet:?'):
                self.download_finished.emit(False, "Invalid magnet link format")
                return
            
            self.progress_update.emit("Validating magnet link...")
            time.sleep(1)  # Give UI time to update
            
            # Check if aria2c is available
            if not self.check_aria2():
                return
            
            self.progress_update.emit("Starting download with aria2c...")
            
            # Create unique download directory for this torrent
            import hashlib
            magnet_hash = hashlib.md5(self.magnet_link.encode()).hexdigest()[:8]
            download_subdir = os.path.join(DOWNLOAD_DIR, f"torrent_{magnet_hash}")
            os.makedirs(download_subdir, exist_ok=True)
            
            # Aria2c command with better parameters and timeout
            cmd = [
                "aria2c",
                self.magnet_link,
                "--dir", download_subdir,
                "--seed-time=0",  # Don't seed after download
                "--file-allocation=none",  # Faster start
                "--continue=true",  # Resume if interrupted
                "--max-connection-per-server=16",
                "--split=16",
                "--min-split-size=1M",
                "--max-concurrent-downloads=1",
                "--summary-interval=2",  # Progress updates every 2 seconds
                "--console-log-level=warn",  # Reduce verbose output
                "--log-level=warn",
                "--bt-tracker-timeout=10",  # Timeout for trackers
                "--bt-tracker-connect-timeout=5",
                "--dht-entry-point-timeout=10",
                "--timeout=30",  # Connection timeout
                "--retry-wait=3",
                "--max-tries=3"
            ]
            
            self.progress_update.emit("Connecting to DHT network and trackers...")
            
            # Start the download process
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Combine stderr with stdout
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # Monitor the download progress with timeout
            download_started = False
            connection_established = False
            start_time = time.time()
            connection_timeout = 60  # 60 seconds to establish connection
            
            while True:
                # Check for timeout
                if not connection_established and (time.time() - start_time) > connection_timeout:
                    self.process.terminate()
                    self.download_finished.emit(False, "Timeout: Could not connect to peers within 60 seconds. The torrent might be dead or have no seeders.")
                    return
                
                output = self.process.stdout.readline()
                if output == '' and self.process.poll() is not None:
                    break
                
                if output:
                    line = output.strip()
                    print(f"Aria2c: {line}")  # Debug output
                    
                    # Look for connection indicators
                    if any(keyword in line.lower() for keyword in ["connecting", "connected", "peer", "seeder"]):
                        if not connection_established:
                            connection_established = True
                            self.progress_update.emit("Connected to peers, starting download...")
                    
                    # Look for download progress
                    if "%" in line and any(keyword in line for keyword in ["DL:", "download", "completed"]):
                        if not download_started:
                            self.progress_update.emit("Download in progress...")
                            download_started = True
                        
                        # Extract percentage if possible
                        try:
                            if "(" in line and "%)" in line:
                                percent_part = line.split("(")[1].split("%)")[0]
                                self.progress_update.emit(f"Downloading... {percent_part}%")
                            elif "%" in line:
                                # Alternative percentage extraction
                                import re
                                percent_match = re.search(r'(\d+)%', line)
                                if percent_match:
                                    self.progress_update.emit(f"Downloading... {percent_match.group(1)}%")
                        except:
                            self.progress_update.emit("Downloading...")
                    
                    # Look for completion
                    elif any(keyword in line.lower() for keyword in ["download complete", "finished", "completed successfully"]):
                        self.progress_update.emit("Download completed! Scanning for music files...")
                        download_started = True
                        break
                    
                    # Look for errors
                    elif any(keyword in line.lower() for keyword in ["error", "failed", "cannot", "unable"]):
                        # Filter out DHT errors which are normal
                        if "dht" not in line.lower() and "server error" not in line.lower():
                            self.download_finished.emit(False, f"Download error: {line}")
                            return
                    
                    # Look for metadata download (indicates torrent is valid)
                    elif "metadata" in line.lower() and "download" in line.lower():
                        connection_established = True
                        self.progress_update.emit("Downloading torrent metadata...")
            
            # Wait for process to finish with timeout
            try:
                stdout, stderr = self.process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                stdout, stderr = self.process.communicate()
            
            if self.process.returncode == 0 or download_started:
                self.progress_update.emit("Scanning downloaded files...")
                # Scan for music files
                music_files = self.scan_for_music_files(download_subdir)
                
                if music_files:
                    self.files_found.emit(music_files)
                    self.download_finished.emit(True, f"Successfully downloaded {len(music_files)} music file(s)!")
                else:
                    # Check if any files were downloaded
                    all_files = []
                    try:
                        for root, dirs, files in os.walk(download_subdir):
                            all_files.extend(files)
                    except:
                        pass
                    
                    if all_files:
                        self.download_finished.emit(False, f"Download completed but no music files found. Downloaded {len(all_files)} file(s) of other types.")
                    else:
                        self.download_finished.emit(False, "No files were downloaded. The torrent might be empty or have no active seeders.")
            else:
                # Provide more specific error messages
                if not connection_established:
                    self.download_finished.emit(False, "Failed to connect to any peers. The torrent might be dead or have no seeders.")
                else:
                    error_msg = stderr or "Unknown download error"
                    self.download_finished.emit(False, f"Download failed: {error_msg}")
                
        except KeyboardInterrupt:
            self.download_finished.emit(False, "Download cancelled by user")
        except Exception as e:
            self.download_finished.emit(False, f"Error during download: {str(e)}")
    
    def check_aria2(self):
        """Check if aria2c is available"""
        try:
            result = subprocess.run(
                ["aria2c", "--version"], 
                capture_output=True, 
                text=True, 
                timeout=5
            )
            if result.returncode == 0:
                self.progress_update.emit("aria2c found, preparing download...")
                return True
            else:
                self.download_finished.emit(False, "aria2c is not working properly")
                return False
        except FileNotFoundError:
            self.download_finished.emit(False, "aria2c not found. Please install aria2")
            return False
        except subprocess.TimeoutExpired:
            self.download_finished.emit(False, "aria2c check timed out")
            return False
    
    def scan_for_music_files(self, directory):
        """Scan directory for music files"""
        music_extensions = {'.flac', '.mp3', '.wav', '.ogg', '.m4a', '.aac', '.wma'}
        music_files = []
        
        try:
            for root, dirs, files in os.walk(directory):
                for file in files:
                    file_lower = file.lower()
                    if any(file_lower.endswith(ext) for ext in music_extensions):
                        full_path = os.path.join(root, file)
                        # Verify file is not empty and readable
                        if os.path.getsize(full_path) > 1024:  # At least 1KB
                            music_files.append(full_path)
                            
        except Exception as e:
            print(f"Error scanning files: {e}")
        
        return music_files
    
    def stop_download(self):
        """Stop the download process"""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


class MusicPlayer(QThread):
    position_changed = pyqtSignal(int)
    duration_changed = pyqtSignal(int)
    track_finished = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        self.current_track = None
        self.is_playing = False
        self.is_paused = False
        self.position = 0
        self.duration = 0
        self.volume = 0.7
        pygame.mixer.music.set_volume(self.volume)
        
    def load_track(self, file_path):
        try:
            # Verify file exists and is readable
            if not os.path.exists(file_path):
                print(f"File not found: {file_path}")
                return False
                
            if os.path.getsize(file_path) == 0:
                print(f"File is empty: {file_path}")
                return False
            
            self.current_track = file_path
            pygame.mixer.music.load(file_path)
            self.get_duration()
            print(f"Successfully loaded: {file_path}")
            return True
        except Exception as e:
            print(f"Error loading track {file_path}: {e}")
            return False
    
    def get_duration(self):
        if self.current_track:
            try:
                if self.current_track.lower().endswith('.flac'):
                    audio = FLAC(self.current_track)
                elif self.current_track.lower().endswith('.mp3'):
                    audio = MP3(self.current_track)
                else:
                    # For other formats, estimate or use default
                    self.duration = 180  # 3 minutes default
                    self.duration_changed.emit(self.duration)
                    return
                
                if audio and hasattr(audio, 'info') and hasattr(audio.info, 'length'):
                    self.duration = int(audio.info.length)
                    self.duration_changed.emit(self.duration)
                else:
                    self.duration = 180  # Default 3 minutes
                    self.duration_changed.emit(self.duration)
            except Exception as e:
                print(f"Error getting duration: {e}")
                self.duration = 180  # Default 3 minutes
                self.duration_changed.emit(self.duration)
    
    def play(self):
        if self.current_track:
            try:
                if self.is_paused:
                    pygame.mixer.music.unpause()
                    self.is_paused = False
                else:
                    pygame.mixer.music.play()
                self.is_playing = True
                if not self.isRunning():
                    self.start()
                return True
            except Exception as e:
                print(f"Error playing track: {e}")
                return False
        return False
    
    def pause(self):
        if self.is_playing:
            pygame.mixer.music.pause()
            self.is_paused = True
            self.is_playing = False
    
    def stop(self):
        pygame.mixer.music.stop()
        self.is_playing = False
        self.is_paused = False
        self.position = 0
        self.position_changed.emit(0)
    
    def set_volume(self, volume):
        self.volume = volume / 100.0
        pygame.mixer.music.set_volume(self.volume)
    
    def run(self):
        while self.is_playing:
            if not pygame.mixer.music.get_busy() and not self.is_paused:
                self.track_finished.emit()
                break
            
            # Update position (approximate)
            if not self.is_paused:
                self.position += 1
                self.position_changed.emit(self.position)
            
            time.sleep(1)


class GlowButton(QPushButton):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #4a90e2, stop:1 #357abd);
                border: 2px solid #2c5aa0;
                border-radius: 15px;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                min-height: 20px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #5ba0f2, stop:1 #4080cd);
                border: 2px solid #3d6bb0;
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #357abd, stop:1 #2c5aa0);
                border: 2px solid #1e3f70;
            }
            QPushButton:disabled {
                background: #666666;
                border: 2px solid #444444;
                color: #999999;
            }
        """)


class MagniTunesPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MagniTunes - FLAC Music Player")
        self.setGeometry(100, 100, 900, 700)
        
        # Set dark theme
        self.setStyleSheet("""
            QMainWindow {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #1a1a1a, stop:1 #2d2d2d);
                color: #ffffff;
            }
            QLabel {
                color: #ffffff;
                font-size: 12px;
            }
            QLineEdit {
                background: #3d3d3d;
                border: 2px solid #555555;
                border-radius: 10px;
                padding: 8px;
                color: #ffffff;
                font-size: 12px;
            }
            QLineEdit:focus {
                border: 2px solid #4a90e2;
            }
            QListWidget {
                background: #2d2d2d;
                border: 2px solid #555555;
                border-radius: 10px;
                color: #ffffff;
                font-size: 11px;
                selection-background-color: #4a90e2;
                alternate-background-color: #3d3d3d;
            }
            QListWidget::item {
                padding: 8px;
                border-bottom: 1px solid #444444;
            }
            QListWidget::item:hover {
                background: #404040;
            }
            QListWidget::item:selected {
                background: #4a90e2;
            }
            QSlider::groove:horizontal {
                border: 1px solid #555555;
                height: 8px;
                background: #3d3d3d;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #4a90e2;
                border: 2px solid #357abd;
                width: 18px;
                height: 18px;
                border-radius: 9px;
                margin: -5px 0;
            }
            QSlider::sub-page:horizontal {
                background: #4a90e2;
                border-radius: 4px;
            }
            QProgressBar {
                border: 2px solid #555555;
                border-radius: 5px;
                text-align: center;
                background: #3d3d3d;
                color: white;
            }
            QProgressBar::chunk {
                background-color: #4a90e2;
                border-radius: 3px;
            }
        """)
        
        self.setup_ui()
        self.setup_player()
        
        self.current_playlist = []
        self.current_track_index = 0
        self.shuffle_mode = False
        self.repeat_mode = False
        self.download_worker = None

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)
        
        # Title
        title_label = QLabel("🎵 MagniTunes")
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet("""
            QLabel {
                font-size: 24px;
                font-weight: bold;
                color: #4a90e2;
                margin-bottom: 10px;
            }
        """)
        main_layout.addWidget(title_label)
        
        # Magnet link input section
        input_group = QGroupBox("Magnet Link Download")
        input_group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: bold;
                color: #4a90e2;
                border: 2px solid #555555;
                border-radius: 10px;
                margin-top: 10px;
                padding-top: 15px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        input_layout = QVBoxLayout(input_group)
        
        # Magnet link input
        self.magnet_input = QLineEdit()
        self.magnet_input.setPlaceholderText("Paste your magnet link here (must start with 'magnet:?')...")
        input_layout.addWidget(self.magnet_input)
        
        # Progress bar for downloads
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        input_layout.addWidget(self.progress_bar)
        
        # Download status label
        self.download_status = QLabel("")
        self.download_status.setStyleSheet("color: #4a90e2; font-weight: bold;")
        self.download_status.setVisible(False)
        input_layout.addWidget(self.download_status)
        
        # Buttons
        button_layout = QHBoxLayout()
        self.download_button = GlowButton("🔗 Download & Add to Playlist")
        self.download_button.clicked.connect(self.start_download)
        button_layout.addWidget(self.download_button)
        
        self.stop_download_button = GlowButton("⏹️ Stop Download")
        self.stop_download_button.clicked.connect(self.stop_download)
        self.stop_download_button.setEnabled(False)
        button_layout.addWidget(self.stop_download_button)
        
        self.clear_button = GlowButton("🗑️ Clear Playlist")
        self.clear_button.clicked.connect(self.clear_playlist)
        button_layout.addWidget(self.clear_button)
        
        # Test button for local files
        self.test_button = GlowButton("🧪 Add Test Files")
        self.test_button.clicked.connect(self.add_test_files)
        button_layout.addWidget(self.test_button)
        
        # Test magnet button
        self.test_magnet_button = GlowButton("🎵 Try Sample Magnet")
        self.test_magnet_button.clicked.connect(self.load_sample_magnet)
        button_layout.addWidget(self.test_magnet_button)
        
        input_layout.addLayout(button_layout)
        main_layout.addWidget(input_group)
        
        # Content area
        content_layout = QHBoxLayout()
        
        # Left side - Playlist
        playlist_group = QGroupBox("Playlist")
        playlist_group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: bold;
                color: #4a90e2;
                border: 2px solid #555555;
                border-radius: 10px;
                margin-top: 10px;
                padding-top: 15px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        playlist_layout = QVBoxLayout(playlist_group)
        
        self.playlist_widget = QListWidget()
        self.playlist_widget.itemDoubleClicked.connect(self.play_selected_track)
        playlist_layout.addWidget(self.playlist_widget)
        
        content_layout.addWidget(playlist_group, 1)
        
        # Right side - Player controls and info
        player_group = QGroupBox("Now Playing")
        player_group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: bold;
                color: #4a90e2;
                border: 2px solid #555555;
                border-radius: 10px;
                margin-top: 10px;
                padding-top: 15px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        player_layout = QVBoxLayout(player_group)
        
        # Track info
        self.track_info_label = QLabel("No track loaded")
        self.track_info_label.setAlignment(Qt.AlignCenter)
        self.track_info_label.setStyleSheet("""
            QLabel {
                font-size: 16px;
                font-weight: bold;
                color: #ffffff;
                padding: 10px;
                background: #3d3d3d;
                border-radius: 8px;
                margin-bottom: 10px;
            }
        """)
        player_layout.addWidget(self.track_info_label)
        
        # Progress bar
        progress_layout = QHBoxLayout()
        self.current_time_label = QLabel("00:00")
        self.progress_slider = QSlider(Qt.Horizontal)
        self.progress_slider.sliderPressed.connect(self.on_slider_pressed)
        self.progress_slider.sliderReleased.connect(self.on_slider_released)
        self.total_time_label = QLabel("00:00")
        
        progress_layout.addWidget(self.current_time_label)
        progress_layout.addWidget(self.progress_slider)
        progress_layout.addWidget(self.total_time_label)
        player_layout.addLayout(progress_layout)
        
        # Control buttons
        controls_layout = QHBoxLayout()
        
        self.shuffle_button = GlowButton("🔀")
        self.shuffle_button.setCheckable(True)
        self.shuffle_button.clicked.connect(self.toggle_shuffle)
        controls_layout.addWidget(self.shuffle_button)
        
        self.prev_button = GlowButton("⏮️")
        self.prev_button.clicked.connect(self.previous_track)
        controls_layout.addWidget(self.prev_button)
        
        self.play_pause_button = GlowButton("▶️")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        controls_layout.addWidget(self.play_pause_button)
        
        self.stop_button = GlowButton("⏹️")
        self.stop_button.clicked.connect(self.stop_playback)
        controls_layout.addWidget(self.stop_button)
        
        self.next_button = GlowButton("⏭️")
        self.next_button.clicked.connect(self.next_track)
        controls_layout.addWidget(self.next_button)
        
        self.repeat_button = GlowButton("🔁")
        self.repeat_button.setCheckable(True)
        self.repeat_button.clicked.connect(self.toggle_repeat)
        controls_layout.addWidget(self.repeat_button)
        
        player_layout.addLayout(controls_layout)
        
        # Volume control
        volume_layout = QHBoxLayout()
        volume_layout.addWidget(QLabel("🔊"))
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setMinimum(0)
        self.volume_slider.setMaximum(100)
        self.volume_slider.setValue(70)
        self.volume_slider.valueChanged.connect(self.change_volume)
        volume_layout.addWidget(self.volume_slider)
        player_layout.addLayout(volume_layout)
        
        content_layout.addWidget(player_group, 1)
        main_layout.addLayout(content_layout)
        
        # Status bar
        self.status_label = QLabel(f"Ready on {platform.system()}. Add some music!")
        self.status_label.setStyleSheet("""
            QLabel {
                background: #3d3d3d;
                border: 1px solid #555555;
                border-radius: 5px;
                padding: 5px;
                color: #ffffff;
            }
        """)
        main_layout.addWidget(self.status_label)

    def setup_player(self):
        self.music_player = MusicPlayer()
        self.music_player.position_changed.connect(self.update_position)
        self.music_player.duration_changed.connect(self.update_duration)
        self.music_player.track_finished.connect(self.on_track_finished)

    def start_download(self):
        magnet_link = self.magnet_input.text().strip()
        if not magnet_link:
            QMessageBox.warning(self, "Invalid Input", "Please enter a magnet link.")
            return
        
        if not magnet_link.startswith('magnet:?'):
            QMessageBox.warning(self, "Invalid Magnet Link", 
                              "Please enter a valid magnet link starting with 'magnet:?'")
            return

        # Disable download button and enable stop button
        self.download_button.setEnabled(False)
        self.stop_download_button.setEnabled(True)
        
        # Show progress elements
        self.progress_bar.setVisible(True)
        self.download_status.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminate progress
        
        # Start download in worker thread
        self.download_worker = DownloadWorker(magnet_link)
        self.download_worker.progress_update.connect(self.update_download_progress)
        self.download_worker.download_finished.connect(self.on_download_finished)
        self.download_worker.files_found.connect(self.add_files_to_playlist)
        self.download_worker.start()

    def stop_download(self):
        if self.download_worker:
            self.download_worker.stop_download()
            self.download_worker.wait(3000)  # Wait up to 3 seconds
            
        self.reset_download_ui()
        self.status_label.setText("Download stopped by user")

    def update_download_progress(self, message):
        self.download_status.setText(message)
        self.status_label.setText(message)

    def on_download_finished(self, success, message):
        self.reset_download_ui()
        
        if success:
            self.status_label.setText(message)
            # Show success notification
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Information)
            msg.setWindowTitle("Download Complete")
            msg.setText(message)
            msg.setInformativeText("Files have been added to your playlist.")
            msg.exec_()
        else:
            self.status_label.setText(f"Download failed: {message}")
            # Show detailed error message
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("Download Failed")
            msg.setText("The download could not be completed.")
            msg.setDetailedText(f"Error details:\n{message}\n\n"
                               f"Common causes:\n"
                               f"• Dead torrent (no seeders)\n"
                               f"• Network connectivity issues\n"
                               f"• Malformed magnet link\n"
                               f"• Firewall blocking connections\n\n"
                               f"Try:\n"
                               f"• A different magnet link\n"
                               f"• Checking your network connection\n"
                               f"• Using the 'Try Sample Magnet' button for testing")
            msg.exec_()

    def add_files_to_playlist(self, file_paths):
        """Add downloaded music files to playlist"""
        for file_path in file_paths:
            if file_path not in self.current_playlist:
                self.current_playlist.append(file_path)
                self.add_to_playlist_widget(file_path)
        
        self.status_label.setText(f"Added {len(file_paths)} music file(s) to playlist")

    def reset_download_ui(self):
        """Reset download UI elements"""
        self.download_button.setEnabled(True)
        self.stop_download_button.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.download_status.setVisible(False)
        self.download_worker = None

    def load_sample_magnet(self):
        """Load a sample magnet link for testing"""
        # This is a Creative Commons music torrent that should work
        sample_magnet = "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Fexplodie.org%3A6969&tr=udp%3A%2F%2Ftracker.coppersurfer.tk%3A6969&tr=udp%3A%2F%2Ftracker.empire-js.us%3A1337&tr=udp%3A%2F%2Ftracker.leechers-paradise.org%3A6969&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337&tr=wss%3A%2F%2Ftracker.btorrent.xyz&tr=wss%3A%2F%2Ftracker.fastcast.nz&tr=wss%3A%2F%2Ftracker.openwebtorrent.com"
        
        reply = QMessageBox.question(self, "Sample Magnet Link", 
                                   "This will load a Creative Commons test torrent.\n"
                                   "Note: This is a video file, not music, but it will test the download functionality.\n\n"
                                   "Do you want to proceed?",
                                   QMessageBox.Yes | QMessageBox.No, 
                                   QMessageBox.Yes)
        
        if reply == QMessageBox.Yes:
            self.magnet_input.setText(sample_magnet)
            self.status_label.setText("Sample magnet link loaded. Click 'Download' to test.")

    def add_test_files(self):
        """Add any existing audio files from the download directory for testing"""
        music_extensions = {'.flac', '.mp3', '.wav', '.ogg', '.m4a', '.aac', '.wma'}
        found_files = []
        
        try:
            if os.path.exists(DOWNLOAD_DIR):
                for root, dirs, files in os.walk(DOWNLOAD_DIR):
                    for file in files:
                        file_lower = file.lower()
                        if any(file_lower.endswith(ext) for ext in music_extensions):
                            full_path = os.path.join(root, file)
                            if os.path.getsize(full_path) > 1024 and full_path not in self.current_playlist:
                                found_files.append(full_path)
                                
            if found_files:
                self.add_files_to_playlist(found_files)
            else:
                QMessageBox.information(self, "No Files Found", 
                                      f"No audio files found in {DOWNLOAD_DIR}")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Error scanning for files: {e}")

    def add_to_playlist_widget(self, file_path):
        filename = os.path.basename(file_path)
        
        # Try to get metadata
        try:
            if file_path.lower().endswith('.flac'):
                audio = FLAC(file_path)
            elif file_path.lower().endswith('.mp3'):
                audio = MP3(file_path)
            else:
                audio = None
            
            if audio and audio.get('title'):
                title = audio['title'][0] if isinstance(audio['title'], list) else audio['title']
                artist = audio.get('artist', ['Unknown Artist'])
                artist = artist[0] if isinstance(artist, list) else artist
                display_name = f"♪ {artist} - {title}"
            else:
                display_name = f"♪ {filename}"
        except:
            display_name = f"♪ {filename}"
        
        item = QListWidgetItem(display_name)
        item.setData(Qt.UserRole, file_path)
        item.setToolTip(file_path)  # Show full path on hover
        self.playlist_widget.addItem(item)

    def play_selected_track(self, item):
        file_path = item.data(Qt.UserRole)
        if file_path in self.current_playlist:
            self.current_track_index = self.current_playlist.index(file_path)
            self.load_and_play_track(file_path)

    def load_and_play_track(self, file_path):
        if not os.path.exists(file_path):
            QMessageBox.warning(self, "File Not Found", f"Could not find file: {file_path}")
            return
            
        if self.music_player.load_track(file_path):
            self.update_track_info(file_path)
            if self.music_player.play():
                self.play_pause_button.setText("⏸️")
                self.status_label.setText(f"Playing: {os.path.basename(file_path)}")
            else:
                QMessageBox.warning(self, "Playback Error", "Could not start playback")
        else:
            QMessageBox.warning(self, "Load Error", "Could not load the audio file")

    def update_track_info(self, file_path):
        filename = os.path.basename(file_path)
        try:
            if file_path.lower().endswith('.flac'):
                audio = FLAC(file_path)
            elif file_path.lower().endswith('.mp3'):
                audio = MP3(file_path)
            else:
                audio = None
            
            if audio:
                title = audio.get('title', [filename])
                title = title[0] if isinstance(title, list) else title
                
                artist = audio.get('artist', ['Unknown Artist'])
                artist = artist[0] if isinstance(artist, list) else artist
                
                album = audio.get('album', ['Unknown Album'])
                album = album[0] if isinstance(album, list) else album
                
                self.track_info_label.setText(f"🎵 {title}\n👤 {artist}\n💿 {album}")
            else:
                self.track_info_label.setText(f"🎵 {filename}")
        except Exception as e:
            print(f"Error reading metadata: {e}")
            self.track_info_label.setText(f"🎵 {filename}")

    def toggle_play_pause(self):
        if self.music_player.is_playing:
            self.music_player.pause()
            self.play_pause_button.setText("▶️")
            self.status_label.setText("Paused")
        else:
            if self.current_playlist and self.current_track_index < len(self.current_playlist):
                if self.music_player.current_track is None:
                    self.load_and_play_track(self.current_playlist[self.current_track_index])
                else:
                    if self.music_player.play():
                        self.play_pause_button.setText("⏸️")
                        self.status_label.setText("Playing")
            else:
                QMessageBox.information(self, "No Music", "Please add some music to the playlist first")

    def stop_playback(self):
        self.music_player.stop()
        self.play_pause_button.setText("▶️")
        self.status_label.setText("Stopped")

    def next_track(self):
        if not self.current_playlist:
            QMessageBox.information(self, "No Music", "Playlist is empty")
            return
        
        if self.shuffle_mode:
            self.current_track_index = random.randint(0, len(self.current_playlist) - 1)
        else:
            self.current_track_index = (self.current_track_index + 1) % len(self.current_playlist)
        
        self.load_and_play_track(self.current_playlist[self.current_track_index])
        
        # Highlight current track in playlist
        self.highlight_current_track()

    def previous_track(self):
        if not self.current_playlist:
            QMessageBox.information(self, "No Music", "Playlist is empty")
            return
        
        if self.shuffle_mode:
            self.current_track_index = random.randint(0, len(self.current_playlist) - 1)
        else:
            self.current_track_index = (self.current_track_index - 1) % len(self.current_playlist)
        
        self.load_and_play_track(self.current_playlist[self.current_track_index])
        
        # Highlight current track in playlist
        self.highlight_current_track()

    def highlight_current_track(self):
        """Highlight the currently playing track in the playlist"""
        if self.current_track_index < len(self.current_playlist):
            current_file = self.current_playlist[self.current_track_index]
            for i in range(self.playlist_widget.count()):
                item = self.playlist_widget.item(i)
                if item.data(Qt.UserRole) == current_file:
                    self.playlist_widget.setCurrentRow(i)
                    break

    def toggle_shuffle(self):
        self.shuffle_mode = not self.shuffle_mode
        if self.shuffle_mode:
            self.shuffle_button.setStyleSheet(self.shuffle_button.styleSheet() + 
                                            "background-color: #4a90e2 !important;")
            self.status_label.setText("Shuffle mode enabled")
        else:
            self.shuffle_button.setStyleSheet(self.shuffle_button.styleSheet().replace(
                "background-color: #4a90e2 !important;", ""))
            self.status_label.setText("Shuffle mode disabled")

    def toggle_repeat(self):
        self.repeat_mode = not self.repeat_mode
        if self.repeat_mode:
            self.repeat_button.setStyleSheet(self.repeat_button.styleSheet() + 
                                           "background-color: #4a90e2 !important;")
            self.status_label.setText("Repeat mode enabled")
        else:
            self.repeat_button.setStyleSheet(self.repeat_button.styleSheet().replace(
                "background-color: #4a90e2 !important;", ""))
            self.status_label.setText("Repeat mode disabled")

    def change_volume(self, value):
        self.music_player.set_volume(value)
        self.status_label.setText(f"Volume: {value}%")

    def update_position(self, position):
        if not hasattr(self, 'slider_pressed') or not self.slider_pressed:
            self.progress_slider.setValue(position)
            self.current_time_label.setText(self.format_time(position))

    def update_duration(self, duration):
        self.progress_slider.setMaximum(duration)
        self.total_time_label.setText(self.format_time(duration))

    def on_slider_pressed(self):
        self.slider_pressed = True

    def on_slider_released(self):
        self.slider_pressed = False
        # Note: Seeking would require more complex implementation with different audio library

    def on_track_finished(self):
        if self.repeat_mode:
            self.load_and_play_track(self.current_playlist[self.current_track_index])
        else:
            self.next_track()

    def clear_playlist(self):
        reply = QMessageBox.question(self, "Clear Playlist", 
                                   "Are you sure you want to clear the entire playlist?",
                                   QMessageBox.Yes | QMessageBox.No, 
                                   QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            self.playlist_widget.clear()
            self.current_playlist.clear()
            self.stop_playback()
            self.track_info_label.setText("No track loaded")
            self.status_label.setText("Playlist cleared")

    def format_time(self, seconds):
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes:02d}:{seconds:02d}"

    def closeEvent(self, event):
        """Handle application closing"""
        if self.download_worker and self.download_worker.isRunning():
            self.download_worker.stop_download()
            self.download_worker.wait(3000)
        
        self.music_player.stop()
        if self.music_player.isRunning():
            self.music_player.wait(1000)
        
        event.accept()


@app.route("/stream/<path:filename>")
def stream_audio(filename):
    try:
        return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=False)
    except FileNotFoundError:
        return "Audio file not found", 404


def main():
    import sys
    
    app_qt = QApplication(sys.argv)
    app_qt.setApplicationName("MagniTunes")
    app_qt.setApplicationVersion("1.0")
    app_qt.setApplicationDisplayName("MagniTunes - FLAC Music Player")
    
    # Check for required dependencies
    missing_deps = []
    
    try:
        import pygame
    except ImportError:
        missing_deps.append("pygame")
    
    try:
        import mutagen
    except ImportError:
        missing_deps.append("mutagen")
    
    if missing_deps:
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowTitle("Missing Dependencies")
        msg.setText("Required Python packages are missing:")
        msg.setDetailedText(f"Please install: {', '.join(missing_deps)}\n\n"
                           f"Run: pip install {' '.join(missing_deps)}")
        msg.exec_()
        return
    
    # Check for aria2c
    try:
        result = subprocess.run(["aria2c", "--version"], capture_output=True, timeout=5)
        if result.returncode != 0:
            raise FileNotFoundError
    except (FileNotFoundError, subprocess.TimeoutExpired):
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("aria2c Not Found")
        msg.setText("aria2c is required for downloading torrents.")
        msg.setInformativeText("You can still use the app with local files, but torrent downloads won't work.")
        msg.setDetailedText(f"Install aria2 for {platform.system()}:\n"
                           f"• Windows: Download from https://aria2.github.io/\n"
                           f"• Linux: sudo apt install aria2\n"
                           f"• macOS: brew install aria2")
        msg.exec_()
    
    window = MagniTunesPlayer()
    window.show()
    
    try:
        sys.exit(app_qt.exec_())
    except KeyboardInterrupt:
        print("Application interrupted by user")


if __name__ == "__main__":
    main()