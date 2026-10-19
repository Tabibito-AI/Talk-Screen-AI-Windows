# Setup Guide / セットアップガイド

## English

### Prerequisites
- Python 3.11 or later
- Windows 10/11 for Windows version
- macOS 10.15 or later for Mac version
- Microphone
- Speaker or headphone

### Installation Steps

1. Clone this repository:
```bash
git clone https://github.com/your-username/Talk-Screen-AI.git
cd Talk-Screen-AI
```

2. Create a virtual environment (recommended):
```bash
python -m venv venv
```

3. Activate the virtual environment:
- Windows:
```bash
.\venv\Scripts\activate
```
- macOS:
```bash
source venv/bin/activate
```

4. Install dependencies:
- For Windows:
```bash
pip install -r requirements.txt
```
- For macOS:
```bash
pip install -r requirements.txt
```

5. Create a `.env` file in the root directory and add your API key:
```
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-3.6-flash

# Optional persistent-assistant tuning
PROACTIVE_COOLDOWN_SEC=90
SCREEN_ANALYSIS_INTERVAL_SEC=20
SPEECH_RMS_THRESHOLD=220
MAX_ENV_OBSERVATIONS=150
MIC_DEVICE_NAME=Logi C270
# MIC_DEVICE_INDEX=1
```

6. Run the application:
- For Windows:
```bash
python src/main_windows.py
```
- For macOS:
```bash
python src/main.py
```

### Usage
1. After starting the application, you will see a "message >" prompt
2. Speak into your microphone or type text to interact with the AI
3. The AI will respond with voice and text
4. Type 'q' to quit the application

### Troubleshooting
- If you encounter audio device errors, check your system's default input/output devices
- Make sure your API key is correctly set in the `.env` file
- For Windows users, ensure you have the latest graphics drivers installed

## 日本語

### 必要条件
- Python 3.11以降
- Windows版の場合：Windows 10/11
- Mac版の場合：macOS 10.15以降
- マイク
- スピーカーまたはヘッドフォン

### インストール手順

1. リポジトリをクローン：
```bash
git clone https://github.com/your-username/Talk-Screen-AI.git
cd Talk-Screen-AI
```

2. 仮想環境の作成（推奨）：
```bash
python -m venv venv
```

3. 仮想環境の有効化：
- Windows：
```bash
.\venv\Scripts\activate
```
- macOS：
```bash
source venv/bin/activate
```

4. 依存パッケージのインストール：
- Windows：
```bash
pip install -r requirements.txt
```
- macOS：
```bash
pip install -r requirements.txt
```

5. ルートディレクトリに`.env`ファイルを作成し、APIキーを追加：
```
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-3.6-flash

# 任意: 常駐アシスタントの調整値
PROACTIVE_COOLDOWN_SEC=90
SCREEN_ANALYSIS_INTERVAL_SEC=20
SPEECH_RMS_THRESHOLD=220
MAX_ENV_OBSERVATIONS=150
MIC_DEVICE_NAME=Logi C270
# MIC_DEVICE_INDEX=1
```

6. アプリケーションの実行：
- Windows：
```bash
python src/main_windows.py
```
- macOS：
```bash
python src/main.py
```

### 使用方法
1. アプリケーション起動後、"message >"プロンプトが表示されます
2. マイクに向かって話すか、テキストを入力してAIと対話します
3. AIは音声とテキストで応答します
4. 'q'を入力して終了します

### トラブルシューティング
- オーディオデバイスのエラーが発生した場合、システムのデフォルト入出力デバイスを確認してください
- `.env`ファイルにAPIキーが正しく設定されているか確認してください
- Windows版の場合、最新のグラフィックドライバがインストールされているか確認してください
