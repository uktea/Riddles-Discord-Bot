# Linuxセットアップ手順

この手順では、systemdを使用できるUbuntu / Debian系Linuxを例に、Botを専用ユーザーで常時稼働させます。ほかのディストリビューションでは、パッケージ名やコマンドを読み替えてください。

## 1. 事前準備

Python 3.11以上と仮想環境機能をインストールします。

```bash
sudo apt update
sudo apt install python3 python3-venv
python3 --version
```

表示されたバージョンが3.11未満の場合は、そのディストリビューションが提供するPython 3.11以上を導入してから進めてください。

[READMEの「Discord側の準備」](../README.md#discord側の準備)に従い、Discord Botの作成、招待、必要権限の設定も済ませます。

## 2. 専用ユーザーと配置先を用意する

この例では、専用ユーザー名を `discordbot`、配置先を `/opt/riddles-discord-bot` とします。

```bash
sudo useradd --system --user-group --home-dir /opt/riddles-discord-bot --create-home --shell /usr/sbin/nologin discordbot
```

プロジェクト一式を `/opt/riddles-discord-bot` へ配置し、所有者を変更します。次の `/path/to/project` は、実際のソースの場所へ置き換えてください。

```bash
sudo cp -a /path/to/project/. /opt/riddles-discord-bot/
sudo chown -R discordbot:discordbot /opt/riddles-discord-bot
```

既に同名ユーザーや配置先がある場合は、新規作成せず既存環境を確認してください。

## 3. 仮想環境と依存パッケージ

専用ユーザーの権限で仮想環境を作成し、依存パッケージをインストールします。

```bash
sudo -u discordbot python3 -m venv /opt/riddles-discord-bot/.venv
sudo -u discordbot /opt/riddles-discord-bot/.venv/bin/python -m pip install --upgrade pip
sudo -u discordbot /opt/riddles-discord-bot/.venv/bin/python -m pip install -r /opt/riddles-discord-bot/requirements.txt
```

## 4. 環境変数を設定する

`.env.example` を `.env` へコピーし、所有者だけが読めるようにします。

```bash
sudo -u discordbot cp /opt/riddles-discord-bot/.env.example /opt/riddles-discord-bot/.env
sudoedit /opt/riddles-discord-bot/.env
sudo chown discordbot:discordbot /opt/riddles-discord-bot/.env
sudo chmod 600 /opt/riddles-discord-bot/.env
```

内容を次のように設定します。

```dotenv
DISCORD_TOKEN=
DISCORD_GUILD_ID=
DATABASE_PATH=data/riddles.db
LOG_LEVEL=INFO
```

- `DISCORD_TOKEN=` の右側にDeveloper Portalで取得したBotトークンを入力します。
- `DISCORD_GUILD_ID=` の右側に利用するサーバーIDを入力します。
- 通常は `DATABASE_PATH` と `LOG_LEVEL` を変更する必要はありません。

Botトークンが入った `.env` を共有、添付、Gitへコミットしないでください。`.env.example` の `DISCORD_TOKEN` は常に空のままにします。

データ保存先を作成し、専用ユーザーだけがアクセスできるようにします。

```bash
sudo -u discordbot mkdir -p /opt/riddles-discord-bot/data
sudo chmod 700 /opt/riddles-discord-bot/data
```

## 5. 手動で起動確認する

サービスを登録する前に、一度手動で起動します。

```bash
cd /opt/riddles-discord-bot
sudo -u discordbot .venv/bin/python main.py
```

確認する項目:

1. ログにBotのログイン成功が表示される。
2. 対象サーバーで `/status` が実行できる。
3. `/briddle` または `/riddle` で公開スレッドが作成される。
4. スレッド内の「回答する」ボタンから回答できる。

停止する場合は `Ctrl+C` を押します。

## 6. systemdサービスを登録する

次の内容で `/etc/systemd/system/riddles-discord-bot.service` を作成します。

```bash
sudoedit /etc/systemd/system/riddles-discord-bot.service
```

```ini
[Unit]
Description=Riddles Discord Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=discordbot
Group=discordbot
WorkingDirectory=/opt/riddles-discord-bot
EnvironmentFile=/opt/riddles-discord-bot/.env
ExecStart=/opt/riddles-discord-bot/.venv/bin/python /opt/riddles-discord-bot/main.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/riddles-discord-bot/data
UMask=0077

[Install]
WantedBy=multi-user.target
```

この設定では、Botが書き込める場所を `data` ディレクトリへ制限しています。`DATABASE_PATH` を `/opt/riddles-discord-bot/data` の外へ変更する場合は、サービスの `ReadWritePaths` も安全な保存先に合わせて変更する必要があります。

設定を読み込み、サービスを自動起動・開始します。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now riddles-discord-bot
```

状態を確認します。

```bash
sudo systemctl status riddles-discord-bot
```

リアルタイムでログを確認します。

```bash
sudo journalctl -u riddles-discord-bot -f
```

直近100行だけを確認する場合:

```bash
sudo journalctl -u riddles-discord-bot -n 100 --no-pager
```

ログを共有するときは、Botトークン、問題の正解、ユーザー情報などが含まれていないことを確認してください。

## 7. 設定変更と更新

`.env` を変更した場合は、サービスを再起動します。

```bash
sudo systemctl restart riddles-discord-bot
```

依存パッケージを更新する場合:

```bash
sudo systemctl stop riddles-discord-bot
sudo -u discordbot /opt/riddles-discord-bot/.venv/bin/python -m pip install -r /opt/riddles-discord-bot/requirements.txt
sudo systemctl start riddles-discord-bot
```

コードを入れ替える場合も、先にサービスを停止し、所有者が `discordbot:discordbot` のままであることを確認してから再開します。同じBotを手動起動とsystemdの両方から同時に動かさないでください。

## 8. スリープを防止する

デスクトップPCやノートPCをサーバーとして使う場合、画面の自動消灯は問題ありませんが、サスペンド・休止状態になるとBotは停止します。デスクトップ環境の電源設定で、自動サスペンドを無効にしてください。

systemdを使用する環境では、次のコマンドでスリープ関連ターゲットを無効化できます。

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

元に戻す場合:

```bash
sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

ノートPCでふたを閉じて運用する場合は、`/etc/systemd/logind.conf` の `HandleLidSwitch` 設定も関係します。ただし、ふたを閉じた状態での常時稼働は排熱に問題がないことを確認してください。停電後の自動起動が必要な場合は、PCのBIOS / UEFIにあるAC電源復帰設定も確認します。

## 9. 停止・無効化・削除

一時停止:

```bash
sudo systemctl stop riddles-discord-bot
```

OS起動時の自動開始も無効化:

```bash
sudo systemctl disable --now riddles-discord-bot
```

サービス定義を削除する場合は、先に無効化したうえでサービスファイルを削除し、`systemctl daemon-reload` を実行します。Botを今後使用しない、またはトークン漏えいが疑われる場合は、Discord Developer Portalの `Bot` 画面でトークンを再生成してください。
