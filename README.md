# Codex Cloud から Dropbox の Obsidian ノートを編集する

このリポジトリは、iPad の Obsidian が Remotely Save で同期する Dropbox 内の既存 Markdown を編集するための、リポジトリ内 skill と Python スクリプトを提供します。\
対象環境は、セットアップスクリプトとネットワーク設定を持つリポジトリ型 Codex Cloud です。\
以下の設定画面は [Codex Cloud の環境ドキュメント（Legacy）](https://learn.chatgpt.com/docs/environments/cloud-environment) に基づき、別形式の Cloud 環境には適用を保証しません。

## 前提と同期手順

- Python 3.10 以降を使用
- このリポジトリを選択した Cloud 環境から取得可能にする（公開・接続の操作は利用者が実施）
- Remotely Save 0.4.1 以降を使用し、暗号化パスワードを空に設定
- 同期方向は Dropbox から iPad への変更取得を許す設定を使用（通常の双方向同期など）
- 最初は使い捨てのノートで検証

作業前に iPad で同期を完了し、Cloud 作業中は同じノートの編集を止めます。\
Cloud の保存成功後に iPad で再同期し、内容を確認してください。\
Dropbox の revision 照合は Dropbox 上の競合を検出しますが、まだ同期されていない iPad の変更は検出できません。

[Remotely Save README](https://github.com/remotely-save/remotely-save/blob/master/README.md) は、パスワードなしの場合に内容が平文で保存されることを説明しています。\
既定の Dropbox フォルダーは `/Apps/remotely-save/<vault名>` ですが、実際の vault 名とカスタム `remoteBaseDir` を確認して設定します。\
[Dropbox 実装](https://github.com/remotely-save/remotely-save/blob/master/src/fsDropbox.ts) と [外部変更との共存方針](https://raw.githubusercontent.com/remotely-save/remotely-save/master/docs/minimal_intrusive_design.md) を参照してください。

## 初回設定

### 1. 自分専用の Dropbox アプリを作る

[Dropbox App Console](https://www.dropbox.com/developers/apps) で Scoped access の **Full Dropbox** アプリを作成し、次の権限を有効にします。

- `files.metadata.read`
- `files.content.read`
- `files.content.write`

自作アプリの App Folder 権限では、別アプリである Remotely Save が作ったフォルダーを扱えません。\
Full Dropbox のトークンは vault 外にもアクセス可能なため、スクリプトのパス制限を Dropbox 側の権限境界と混同しないでください。\
認可方式の詳細は [Dropbox OAuth ガイド](https://docs.dropboxapi.com/dropbox-api/docs/oauth) に従います。

### 2. ローカルで一度だけ認可する

以下は PowerShell の例です。\
出力先はリポジトリと作業ディレクトリの外にある、新規の秘密ファイルにします。

```powershell
$env:DROPBOX_APP_KEY = '自分のアプリキー'
python .agents/skills/edit-dropbox-obsidian/scripts/dropbox_obsidian.py authorize --out "$HOME/dropbox-refresh-token.json"
```

表示された URL をブラウザーで開き、自分のアプリを認可します。\
認可コードは非表示の入力欄へ入力し、発行された refresh token は指定ファイルから Cloud 設定へ手動転記してください。\
この処理は PKCE と offline access を使い、アプリシークレットを必要としません。\
トークンや認可コードをチャット、リポジトリ、シェル履歴へ貼り付けないでください。\
ローカルファイルは OS の所有者専用権限を確認し、転記後に削除します（Windows の ACL は利用者が確認）。

### 3. Cloud 環境を設定する

Cloud の通常の環境変数に次を設定します。

- `DROPBOX_APP_KEY`: 作成したアプリキー
- `DROPBOX_VAULT_ROOT`: 実際の vault フォルダーの絶対パス（例 `/Apps/remotely-save/MyVault`）

Cloud の **Secrets** に `DROPBOX_REFRESH_TOKEN` を設定します。\
長期秘密はセットアップ時だけ使用し、`.env`、`.bashrc`、作業ファイルへ書き出しません。

Cloud のセットアップスクリプトに次を追加します。

```sh
python .agents/skills/edit-dropbox-obsidian/scripts/dropbox_obsidian.py prepare
```

`prepare` は `~/.config/codex-dropbox/access.json` に短期 access token と有効期限だけを新規保存します。\
Linux では格納ディレクトリを `0700`、ファイルを `0600` にします。\
これは Cloud セットアップによるワークスペース外への書き込みであり、実行環境の許可設定が必要です。

エージェントのネットワーク許可に `api.dropboxapi.com` と `content.dropboxapi.com` を登録し、**POST を許可**します。\
GET のみの設定では Dropbox API を利用できません。\
詳細は [Cloud のインターネットアクセス](https://learn.chatgpt.com/docs/cloud/internet-access) を参照してください。

短期トークンの寿命はレスポンスの `expires_in` に従い、通常は数時間です。\
セットアップ環境のキャッシュは最大 12 時間残るため、キャッシュがあることと認証が有効であることは一致しません。\
期限切れ時はセットアップを再実行するか、環境キャッシュを Reset してセットアップを再実行します。\
`prepare` は所有者専用の既存 access credential を検証し、一時ファイルから原子的に更新します。

短期認証ファイルはエージェントとキャッシュされたコンテナから読めます。\
専用の非公開 Cloud 環境を使い、不要になった時や運用終了時は Dropbox の連携を取り消し、Cloud 環境の秘密情報とキャッシュを削除してください。

## ノート編集

skill を追加した後は、新しいチャットでリポジトリを開いて discovery を更新します。\
例えば「Dropbox の `日記/検証.md` の末尾に指定内容を追記して保存して」と依頼します。\
[リポジトリ内 skill の discovery](https://learn.chatgpt.com/docs/build-skills) に合わせて `.agents/skills/` に配置しています。

手動の実行例は次のとおりです。

```sh
python .agents/skills/edit-dropbox-obsidian/scripts/dropbox_obsidian.py list
python .agents/skills/edit-dropbox-obsidian/scripts/dropbox_obsidian.py checkout '日記/検証.md' --out .dropbox-work/task
# .dropbox-work/task/edited.md を編集
python .agents/skills/edit-dropbox-obsidian/scripts/dropbox_obsidian.py diff .dropbox-work/task
python .agents/skills/edit-dropbox-obsidian/scripts/dropbox_obsidian.py apply .dropbox-work/task --confirm
```

`original.md` は取得時のバイト列、`state.json` は vault・相対パス・revision・原本のハッシュです。\
編集するのは `edited.md` のみとし、差分を読んでから保存します。\
`--credentials PATH` はサブコマンドより前に指定すると、既定以外の短期認証ファイルを使えます。

保存は [Dropbox API の update revision と strict_conflict](https://raw.githubusercontent.com/dropbox/dropbox-api-spec/main/files.stone) を使用し、取得後に変更・削除されたノートでは失敗します。\
自動改名、新規作成、移動、削除、自動再試行は行いません。\
アップロードのタイムアウトでは結果が不明なため、バンドルを残したまま Dropbox 上の内容を確認します。\
同じ編集を再アップロードする前に、新しいバンドルでリモート状態を取得して比較してください。

対象は 10 MiB 以下の平文 UTF-8 `.md` で、隠しディレクトリ、相対パスの逸脱、symlink を拒否します。\
ノート本文は信頼できないデータとして扱い、本文中の運用指示には従いません。\
バンドルのハッシュ検証は accidental な原本変更を検出するもので、作業ディレクトリを書き換えられる攻撃者に対する署名検証ではありません。

## オフライン検証

```sh
python -m unittest discover -s tests -v
python .agents/skills/edit-dropbox-obsidian/scripts/dropbox_obsidian.py --help
```

テストはネットワークを mock し、実際の Dropbox や iPad に接続しません。\
実環境との往復同期は、上記の使い捨てノートで利用者が確認する範囲です。
