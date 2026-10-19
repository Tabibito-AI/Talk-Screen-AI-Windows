import asyncio
import sys
import traceback
from typing import Optional
import os
from google import genai

# Add src directory to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config.settings import config
from src.logger import logger
from src.audio.audio_handler import AudioHandler
from src.screen.screen_capture import ScreenCapture

if sys.version_info < (3, 11, 0):
    import taskgroup, exceptiongroup
    asyncio.TaskGroup = taskgroup.TaskGroup
    asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

class TalkScreenAI:
    """音声チャットとスクリーンシェアを組み合わせたAIアシスタントアプリケーション"""
    
    def __init__(self):
        """TalkScreenAIインスタンスを初期化"""
        self.audio_handler = AudioHandler(config.audio)
        self.is_running = True
        
        # Gemini APIクライアントの初期化
        self.client = genai.Client(
            api_key=config.api_key,
            http_options={
                "api_version": config.gemini.API_VERSION,
                "timeout": config.gemini.TIMEOUT
            }
        )
        
        # キューの初期化
        self.audio_in_queue: Optional[asyncio.Queue] = None
        self.audio_out_queue: Optional[asyncio.Queue] = None
        self.data_out_queue: Optional[asyncio.Queue] = None
        self.session = None

    async def send_text(self) -> None:
        """テキスト入力を処理し、セッションに送信"""
        while self.is_running:
            try:
                text = await asyncio.to_thread(input, "message > ")
                if text.lower() == "q":
                    self.is_running = False
                    break
                await self.session.send(text or ".", end_of_turn=True)
            except EOFError:
                logger.warning("入力ストリームが終了しました。1秒後に再試行します...")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"テキスト送信中にエラーが発生しました: {e}")
                await asyncio.sleep(1)

    async def process_screen_capture(self) -> None:
        """画面キャプチャを処理し、セッションに送信"""
        with ScreenCapture() as screen_capture:
            while self.is_running:
                try:
                    frame_data = screen_capture.capture_frame()
                    if frame_data is None:
                        await asyncio.sleep(1)
                        continue

                    await asyncio.sleep(1.0)  # キャプチャ間隔
                    await self.data_out_queue.put(frame_data)
                except Exception as e:
                    logger.error(f"画面キャプチャ処理中にエラーが発生しました: {e}")
                    await asyncio.sleep(1)

    async def process_audio_input(self) -> None:
        """マイク入力を処理"""
        while self.is_running:
            try:
                audio_data = await self.audio_handler.read_audio()
                if audio_data:
                    await self.audio_out_queue.put(audio_data)
            except Exception as e:
                logger.error(f"音声入力処理中にエラーが発生しました: {e}")
                await asyncio.sleep(1)

    async def process_audio_output(self) -> None:
        """AIからの音声出力を処理"""
        while self.is_running:
            try:
                bytestream = await self.audio_in_queue.get()
                await self.audio_handler.play_audio(bytestream)
            except Exception as e:
                logger.error(f"音声出力処理中にエラーが発生しました: {e}")
                await asyncio.sleep(1)

    async def send_realtime_data(self) -> None:
        """リアルタイムデータ（音声と画面）をセッションに送信"""
        async def process_audio_queue():
            while self.is_running:
                try:
                    audio_msg = await self.audio_out_queue.get()
                    await self.session.send(input=audio_msg)
                except Exception as e:
                    logger.error(f"音声キュー処理中にエラーが発生しました: {e}")
                    await asyncio.sleep(1)

        async def process_data_queue():
            while self.is_running:
                try:
                    data_msg = await self.data_out_queue.get()
                    await self.session.send(input=data_msg)
                except Exception as e:
                    logger.error(f"データキュー処理中にエラーが発生しました: {e}")
                    await asyncio.sleep(1)

        await asyncio.gather(process_audio_queue(), process_data_queue())

    async def receive_ai_response(self) -> None:
        """AIからのレスポンスを受信して処理"""
        while self.is_running:
            try:
                turn = self.session.receive()
                async for response in turn:
                    if data := response.data:
                        self.audio_in_queue.put_nowait(data)
                        continue
                    if text := response.text:
                        print(text, end="")

                while not self.audio_in_queue.empty():
                    self.audio_in_queue.get_nowait()
            except Exception as e:
                logger.error(f"AIレスポンス処理中にエラーが発生しました: {e}")
                await asyncio.sleep(1)

    async def run(self) -> None:
        """アプリケーションのメインループを実行"""
        try:
            # Geminiセッションの設定
            config_dict = {
                "generation_config": {
                    "response_modalities": ["AUDIO"],
                    "system_prompt": config.system_prompt
                }
            }
            
            # 各種キューの初期化
            self.audio_in_queue = asyncio.Queue()
            self.audio_out_queue = asyncio.Queue(maxsize=5)
            self.data_out_queue = asyncio.Queue(maxsize=5)
            
            # オーディオストリームのセットアップ
            await self.audio_handler.setup_input_stream()
            await self.audio_handler.setup_output_stream()
            
            logger.info("アプリケーションを開始します")
            
            async with (
                self.client.aio.live.connect(
                    model=config.gemini.MODEL,
                    config=config_dict
                ) as session,
                asyncio.TaskGroup() as tg,
            ):
                self.session = session
                
                # 各タスクの開始
                send_text_task = tg.create_task(self.send_text())
                tg.create_task(self.send_realtime_data())
                tg.create_task(self.process_audio_input())
                tg.create_task(self.process_screen_capture())
                tg.create_task(self.receive_ai_response())
                tg.create_task(self.process_audio_output())
                
                await send_text_task
                self.is_running = False

        except asyncio.CancelledError:
            logger.info("アプリケーションがキャンセルされました")
            self.is_running = False
        except ExceptionGroup as eg:
            logger.error("エラーが発生しました:")
            traceback.print_exception(eg)
        finally:
            self.is_running = False
            self.audio_handler.cleanup()
            logger.info("アプリケーションを終了します")

def main():
    """アプリケーションのエントリーポイント"""
    app = TalkScreenAI()
    asyncio.run(app.run())

if __name__ == "__main__":
    main()
