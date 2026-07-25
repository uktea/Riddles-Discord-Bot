# なぞなぞBot

Discordサーバー内で、なぞなぞの出題・回答・期限管理を行うBotです。問題ごとに公開スレッドを作り、参加者はスレッド内の「回答する」ボタンから回答します。

V1では次の2種類の出題に対応します。

- `/briddle`: 最初の正解者が出た時点で終了し、正解を発表します。
- `/riddle`: 期限まで複数人の正解を受け付け、期限になった時点で正解を発表します。

問題・期限・サーバー設定はSQLiteに保存されるため、Botを再起動しても処理を再開できます。

## 動作要件

- Python 3.11以上
- Discord Botを追加できるテスト用または運用用サーバー
- Botを動かし続けるWindowsまたはLinux PC
- インターネット接続

主な依存パッケージは `discord.py` と `python-dotenv` です。SQLiteはPython標準ライブラリを使用するため、別途データベースサーバーを用意する必要はありません。

## Discord側の準備

### 1. Botを作成する

1. [Discord Developer Portal](https://discord.com/developers/applications)を開きます。
2. `New Application` からアプリケーションを作成します。
3. 左側の `Bot` を開き、Botユーザーを作成します。
4. `Reset Token` または `View Token` からトークンを取得し、後述する `.env` の `DISCORD_TOKEN` にだけ保存します。

Botトークンはパスワードと同じ秘密情報です。チャット、スクリーンショット、ソースコード、Gitのコミットには絶対に含めないでください。漏えいした可能性がある場合は、Developer Portalで直ちにトークンを再生成してください。

### 2. 回答方式を確認する

このBotの回答は、スレッド内のボタンとModal（入力フォーム）で受け付けます。Developer PortalのPrivileged Gateway Intentsを追加で有効にする必要はありません。

### 3. Botをサーバーへ招待する

Developer Portalの `OAuth2` → `URL Generator` または `Installation` で、次を設定します。

Scopes:

- `bot`
- `applications.commands`

Bot Permissions:

- View Channels
- Send Messages
- Create Public Threads
- Send Messages in Threads
- Read Message History
- Manage Threads
- Use Application Commands

生成されたURLを開き、対象サーバーへBotを追加します。運用チャンネル側で権限を個別に上書きしている場合は、上記の権限がそのチャンネルと公開スレッドでも許可されていることを確認してください。

## セットアップ

OS別の詳しい手順は次を参照してください。

- [Windowsセットアップ](docs/WINDOWS_SETUP.md)
- [Linuxセットアップ](docs/LINUX_SETUP.md)

最小手順は次のとおりです。

```text
python -m venv .venv
```

仮想環境を作成したら、その仮想環境のPythonで依存パッケージをインストールします。実行ファイルの場所はOSによって異なります。

```powershell
# Windows
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
# Linux
./.venv/bin/python -m pip install -r requirements.txt
```

`.env.example` を `.env` という名前でコピーし、必要な値を入力します。

```dotenv
DISCORD_TOKEN=
DISCORD_GUILD_ID=
DATABASE_PATH=data/riddles.db
LOG_LEVEL=INFO
```

`DISCORD_TOKEN=` の右側には取得したBotトークンを入力します。トークンそのものを他のファイルへ貼り付けないでください。

設定後、プロジェクトのルートディレクトリで起動します。

```powershell
# Windows
.\.venv\Scripts\python.exe main.py
```

```bash
# Linux
./.venv/bin/python main.py
```

正常にログインし、スラッシュコマンドの同期が完了したことをログで確認してください。

## 環境変数

| 変数 | 必須 | 既定値 | 説明 |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | はい | なし | Discord Botの秘密トークン。空のままでは起動できません。 |
| `DISCORD_GUILD_ID` | 推奨 | なし | コマンドを同期するサーバーID。テスト用・単一サーバー運用では設定を推奨します。 |
| `DATABASE_PATH` | いいえ | `data/riddles.db` | SQLiteデータベースの保存先。相対パスはプロジェクトルート基準です。 |
| `LOG_LEVEL` | いいえ | `INFO` | ログレベル。`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`を指定できます。 |

サーバーIDはDiscordの「ユーザー設定」→「詳細設定」→「開発者モード」を有効にし、対象サーバーを右クリックして「サーバーIDをコピー」すると取得できます。サーバーID自体は秘密情報ではありません。

`DISCORD_GUILD_ID` を設定すると、そのサーバーへコマンドを同期するため、開発・単一サーバー運用で変更が反映されやすくなります。未設定時はグローバル同期となり、Discord上への反映に時間がかかることがあります。

## コマンド一覧

角括弧の引数は省略可能です。Botからの管理結果や個人データは、原則として実行者だけに見える応答で表示されます。

| コマンド | 利用者 | 説明 |
| --- | --- | --- |
| `/briddle riddle answer [term]` | 全員 | 問題を作成します。最初の正解で即時終了します。 |
| `/riddle riddle answer [term]` | 全員 | 問題を作成します。期限まで複数人の正解を受け付けます。 |
| `/delete riddle_id` | 作成者・管理者 | 指定した問題を取り消し、対応するスレッドを終了します。 |
| `/list` | 全員 | 自分が作成した問題の一覧を、自分だけに表示します。管理者にはサーバー内の全問題を表示します。 |
| `/pref prefid prefvalue` | 管理者 | サーバー単位のBot設定を変更します。 |
| `/status` | 全員 | Botの稼働状況を表示します。 |
| `/mydata` | 全員 | Botが保存している自分に関するデータを確認します。 |
| `/deletemydata confirm:true` | 全員 | 自分に関する保存データを削除します。 |

`riddle` は問題文、`answer` は正解です。`answer` に別解を登録する場合は、`シロナガスクジラ|blue whale` のように半角の `|` で区切ります。問題作成後、正解は終了時まで公開されません。

参加者は作成された公開スレッド内の「回答する」ボタンを押し、表示されたModalへ回答を入力します。`/briddle` では正誤結果が回答者本人だけに表示されます。`/riddle` では期限前に正誤が推測されないよう、回答を受け付けたことだけが本人に表示され、結果は期限時に発表されます。

`term` は次のように、整数と単位を続けて指定します。

- `30m`: 30分
- `2h`: 2時間
- `1d`: 1日

省略時は24時間です。指定できる範囲は1分から30日までです。Discord上の日時は、各ユーザーのクライアントの言語・タイムゾーン設定に合わせて表示されます。

正解判定では大文字・小文字、全角・半角、空白の違いを正規化したうえで完全一致を確認します。部分一致ではありません。

1ユーザーが同時に作成できる未終了問題は、初期状態では最大10件です。管理者は `/pref` で変更できます。

### `/pref` の設定項目

| `prefid` | `prefvalue` | 初期値 | 説明 |
| --- | --- | --- | --- |
| `allowed_channel` | `current` / `none` / チャンネルID / チャンネルメンション | `none` | 出題できるチャンネルを制限します。`current` は実行中のチャンネル、`none` は制限解除です。 |
| `max_active` | `1`〜`100` | `10` | 1ユーザーが同時に保持できる未終了問題数です。 |
| `mention_winners` | `true` / `false` | `true` | 終了時に正解者をメンションするか設定します。 |

Discordの「管理者」権限を持つメンバーだけが `/pref` を実行できます。

### 個人データの削除

`/deletemydata` は誤操作防止のため、`confirm` に `true` を指定した場合だけ実行されます。実行すると、本人が作成した未終了問題は取り消されてスレッドが終了し、ほかの問題に保存された本人の正解者記録も削除されます。この操作は元に戻せません。先に `/mydata` で対象を確認してください。

## データとセキュリティ

- SQLiteデータベースには、問題の処理に必要なDiscordサーバーID・ユーザーID・問題・正解・期限・設定などが保存されます。問題と正解者記録は結果表示に成功した後で削除され、サーバー設定だけが継続して保存されます。
- Botトークン、問題の正解、参加者がModalへ入力した回答本文はログへ出力しません。
- `.env` とSQLiteデータベースは `.gitignore` の対象です。
- `.env.example` には実際のトークンを記入しないでください。
- Botを同じデータベースに対して複数起動しないでください。
- V1には自動バックアップ機能がありません。

Botがサーバーから削除された場合、そのサーバーのローカルデータは退出イベント受信時、または次回起動時の照合で削除されます。個人データは `/mydata` と `/deletemydata` で本人が確認・削除できます。Discord上に利用者自身が投稿したメッセージは、このローカルデータ削除の対象外です。

## 24時間運用について

自宅PCで24時間動かす場合、BotはPCが起動し、スリープしておらず、ネットワークに接続されている間だけ稼働します。画面を消すだけなら問題ありませんが、スリープ・休止状態・シャットダウン中は応答できません。

Windowsではタスクスケジューラ、Linuxではsystemdを利用すると、OS起動時の自動開始と異常終了時の再起動を設定できます。具体的な設定はOS別セットアップ手順を参照してください。

## トラブルシューティング

### スラッシュコマンドが表示されない

- 招待時に `applications.commands` スコープを選択したか確認します。
- `.env` の `DISCORD_GUILD_ID` が対象サーバーのIDか確認します。
- Botを再起動し、コマンド同期のログを確認します。
- グローバル同期の場合は、Discord側への反映に時間がかかることがあります。

### 問題スレッドを作成できない

対象チャンネルで、Botに `View Channels`、`Send Messages`、`Create Public Threads`、`Send Messages in Threads`、`Read Message History`、`Manage Threads` が許可されているか確認します。チャンネル固有の権限上書きにも注意してください。

### Botがログインできない

`.env` の `DISCORD_TOKEN` が空でないか確認します。トークンを再生成した場合は `.env` を更新してBotを再起動します。トークンをログや問い合わせ文へ貼り付けないでください。

### 再起動後に二重応答する

同じBotを複数のターミナル、タスク、systemdサービスから起動していないか確認します。常に1プロセスだけを起動してください。

## V1の対象外

次の機能は将来候補であり、V1には含まれません。

- 問題への画像・音声・その他ファイルの添付
- 問題作成者・回答者ランキング
- 絵文字リアクションによる評価
- Google Sheets / Google Apps Script連携
- Web管理画面
