import asyncio
from typing import Optional, Dict, Any
import pyaudio
import os
from src.config.settings import AudioConfig
from src.logger import logger

class AudioHandler:
    """オーディオの入出力を管理するクラス"""
    
    def __init__(self, config: AudioConfig):
        """
        AudioHandlerインスタンスを初期化
        
        Args:
            config (AudioConfig): オーディオ設定
        """
        self.config = config
        self.pya = pyaudio.PyAudio()
        self.audio_stream: Optional[pyaudio.Stream] = None
        self.play_stream: Optional[pyaudio.Stream] = None
        self.is_running = True

    def _select_input_device(self) -> Dict[str, Any]:
        """環境変数が指定されていれば優先して入力デバイスを選択する"""
        index_raw = os.getenv("MIC_DEVICE_INDEX")
        name_hint = os.getenv("MIC_DEVICE_NAME", "").strip().lower()

        if index_raw:
            try:
                index = int(index_raw)
                info = self.pya.get_device_info_by_index(index)
                if int(info.get("maxInputChannels", 0)) > 0:
                    return info
                logger.warning(f"MIC_DEVICE_INDEX={index} は入力デバイスではありません。デフォルトを使用します。")
            except Exception as e:
                logger.warning(f"MIC_DEVICE_INDEX={index_raw} の解決に失敗: {e}")

        if name_hint:
            try:
                for i in range(self.pya.get_device_count()):
                    info = self.pya.get_device_info_by_index(i)
                    if int(info.get("maxInputChannels", 0)) <= 0:
                        continue
                    if name_hint in str(info.get("name", "")).lower():
                        return info
                logger.warning(f"MIC_DEVICE_NAME='{name_hint}' に一致する入力デバイスが見つかりません。デフォルトを使用します。")
            except Exception as e:
                logger.warning(f"MIC_DEVICE_NAME 解決中にエラー: {e}")

        return self.pya.get_default_input_device_info()

    async def setup_input_stream(self) -> None:
        """マイク入力ストリームをセットアップ"""
        try:
            mic_info = self._select_input_device()
            self.audio_stream = await asyncio.to_thread(
                self.pya.open,
                format=self.config.FORMAT,
                channels=self.config.CHANNELS,
                rate=self.config.SEND_SAMPLE_RATE,
                input=True,
                input_device_index=mic_info["index"],
                frames_per_buffer=self.config.CHUNK_SIZE,
            )
            logger.info(
                "マイク入力ストリームを初期化しました: index=%s name=%s sample_rate=%s",
                int(mic_info.get("index", -1)),
                mic_info.get("name", "unknown"),
                int(mic_info.get("defaultSampleRate", 0)),
            )
        except Exception as e:
            logger.error(f"マイク入力ストリームの初期化に失敗しました: {e}")
            raise

    async def setup_output_stream(self) -> None:
        """オーディオ出力ストリームをセットアップ"""
        try:
            self.play_stream = await asyncio.to_thread(
                self.pya.open,
                format=self.config.FORMAT,
                channels=self.config.CHANNELS,
                rate=self.config.RECEIVE_SAMPLE_RATE,
                output=True,
            )
            logger.info("オーディオ出力ストリームを初期化しました")
        except Exception as e:
            logger.error(f"オーディオ出力ストリームの初期化に失敗しました: {e}")
            raise

    async def read_audio(self) -> Optional[Dict[str, Any]]:
        """
        マイクから音声データを読み取ります
        
        Returns:
            Optional[Dict[str, Any]]: 音声データとMIMEタイプを含む辞書、エラー時はNone
        """
        if not self.audio_stream:
            logger.error("オーディオ入力ストリームが初期化されていません")
            return None

        try:
            kwargs = {"exception_on_overflow": False} if __debug__ else {}
            data = await asyncio.to_thread(
                self.audio_stream.read,
                self.config.CHUNK_SIZE,
                **kwargs
            )
            return {"data": data, "mime_type": "audio/pcm"}
        except Exception as e:
            logger.error(f"音声データの読み取り中にエラーが発生しました: {e}")
            return None

    async def play_audio(self, bytestream: bytes) -> bool:
        """
        音声データを再生します
        
        Args:
            bytestream (bytes): 再生する音声データ
            
        Returns:
            bool: 再生成功時True、失敗時False
        """
        if not self.play_stream:
            logger.error("オーディオ出力ストリームが初期化されていません")
            return False

        try:
            await asyncio.to_thread(self.play_stream.write, bytestream)
            return True
        except Exception as e:
            logger.error(f"音声データの再生中にエラーが発生しました: {e}")
            return False

    def cleanup(self) -> None:
        """オーディオストリームとPyAudioインスタンスをクリーンアップ"""
        self.is_running = False
        
        if self.audio_stream:
            self.audio_stream.stop_stream()
            self.audio_stream.close()
            
        if self.play_stream:
            self.play_stream.stop_stream()
            self.play_stream.close()
            
        self.pya.terminate()
        logger.info("オーディオリソースをクリーンアップしました")
