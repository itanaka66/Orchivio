# Orchivio Tutorial / チュートリアル

**Copyright (C) 2026 株式会社エージーネディア / agNedia Inc. — GPL-3.0-or-later**

---

## English

### What you will build

In this tutorial you will create a small **Inventory** application — a product catalogue with a list page, a create form, and an edit form — using the Orchivio Studio.

### Prerequisites

- Orchivio running locally (`docker compose up --build`) or on a server
- A browser pointed at `http://localhost` (or your server's address)

### Step 1 — Sign up

1. Open the Studio.
2. Click **Sign up**, enter your email address and a strong password, and submit.
3. You are signed in automatically and land on the Studio home screen.

### Step 2 — Create an organization and project

1. Click **Admin** in the top bar.
2. Under **Organizations**, click **New organization**.  
   Enter `Acme` as the name and `acme` as the slug, then click **Create**.
3. Under **Projects**, click **New project**.  
   Enter `Inventory` as the name and `inventory` as the slug, then click **Create**.

### Step 3 — Create an application

1. Select the **Acme** organization and the **Inventory** project in the Browser panel.
2. Click **New application**.  
   Enter key `inventory`, name `Inventory`, then click **Create**.
3. The application appears in the Application drop-down.

### Step 4 — Add an entity with the AI Builder

1. Select **Inventory** in the Application drop-down.
2. Click **Build with AI**.
3. In the text box, type:
   > Add a `Product` entity with fields: `name` (string, required), `sku` (string, unique), `price` (number), `in_stock` (boolean, default true).
4. Click **Propose directly**.  
   The AI Builder creates a proposal containing the new entity.

### Step 5 — Review and approve the proposal

1. Click **Review proposals**.
2. You will see one pending proposal at approval level **A1** (low risk, one reviewer).
3. Click the proposal row to see the diff — one new entity with four fields.
4. Click **Approve**.  
   The proposal is merged into the application model.

### Step 6 — Add pages with the Page Designer

1. Click **Open the page designer**.
2. Click **New page** next to the `default` module.
3. Fill in the form:
   - Key: `product-list`, Path: `/products`, Type: `list`, Entity: `Product`
4. Click **Save as proposal** and approve the resulting proposal.
5. Repeat for a create form:
   - Key: `product-create`, Path: `/products/new`, Type: `form`, Mode: `create`, Entity: `Product`
6. Repeat for an edit form:
   - Key: `product-edit`, Path: `/products/:id/edit`, Type: `form`, Mode: `edit`, Entity: `Product`

### Step 7 — Preview the application

1. Click **Open the preview**.
2. You can browse the product list, create a new product, and edit it.
3. Changes made in the preview are isolated; they do not affect other applications.

### Next steps

- Invite team members in the **Admin** screen.
- Use the **Model Explorer** to inspect entities, pages, and security rules.
- Enable the AI Builder with an Anthropic key (see README.md) to generate more complex proposals.
- Read `docs/APP-SECURITY.md` to add roles and row-level access rules.

---

## 日本語

### このチュートリアルで作るもの

Orchivio Studio を使って、商品カタログのシンプルな **Inventory** アプリケーションを作成します。商品の一覧ページ、新規作成フォーム、編集フォームの3画面を持つアプリです。

### 前提条件

- Orchivio がローカル (`docker compose up --build`) またはサーバ上で起動していること
- ブラウザで `http://localhost`（またはサーバのアドレス）を開けること

### ステップ 1 — アカウント登録

1. Studio を開きます。
2. **Sign up** をクリックし、メールアドレスと強力なパスワードを入力してサブミットします。
3. 自動でサインインされ、Studio のホーム画面に移ります。

### ステップ 2 — 組織とプロジェクトを作成する

1. トップバーの **Admin** をクリックします。
2. **Organizations** の下の **New organization** をクリックします。  
   名前に `Acme`、スラッグに `acme` を入力して **Create** をクリックします。
3. **Projects** の下の **New project** をクリックします。  
   名前に `Inventory`、スラッグに `inventory` を入力して **Create** をクリックします。

### ステップ 3 — アプリケーションを作成する

1. Browser パネルで **Acme** 組織と **Inventory** プロジェクトを選択します。
2. **New application** をクリックします。  
   キーに `inventory`、名前に `Inventory` を入力して **Create** をクリックします。
3. Application ドロップダウンにアプリが表示されます。

### ステップ 4 — AI Builder でエンティティを追加する

1. Application ドロップダウンで **Inventory** を選択します。
2. **Build with AI** をクリックします。
3. テキストボックスに次のように入力します：
   > `Product` エンティティをフィールド付きで追加してください：`name`（string、必須）、`sku`（string、ユニーク）、`price`（number）、`in_stock`（boolean、デフォルト true）。
4. **Propose directly** をクリックします。  
   AI Builder が新しいエンティティを含むプロポーザルを作成します。

### ステップ 5 — プロポーザルをレビューして承認する

1. **Review proposals** をクリックします。
2. 承認レベル **A1**（低リスク、1人のレビュアーで承認可）の保留中プロポーザルが1件表示されます。
3. プロポーザル行をクリックして差分を確認します — フィールド4つを持つ新規エンティティが1件追加されています。
4. **Approve** をクリックします。  
   プロポーザルがアプリケーションモデルにマージされます。

### ステップ 6 — Page Designer でページを追加する

1. **Open the page designer** をクリックします。
2. `default` モジュールの横の **New page** をクリックします。
3. 以下の内容を入力します：
   - Key: `product-list`、Path: `/products`、Type: `list`、Entity: `Product`
4. **Save as proposal** をクリックし、作成されたプロポーザルを承認します。
5. 作成フォーム用に繰り返します：
   - Key: `product-create`、Path: `/products/new`、Type: `form`、Mode: `create`、Entity: `Product`
6. 編集フォーム用に繰り返します：
   - Key: `product-edit`、Path: `/products/:id/edit`、Type: `form`、Mode: `edit`、Entity: `Product`

### ステップ 7 — アプリケーションをプレビューする

1. **Open the preview** をクリックします。
2. 商品一覧の閲覧、新規商品の作成、編集ができます。
3. プレビューで行った変更は他のアプリケーションには影響しません。

### 次のステップ

- **Admin** 画面でチームメンバーを招待します。
- **Model Explorer** でエンティティ、ページ、セキュリティルールを確認します。
- README.md の手順に従って Anthropic キーを設定し、AI Builder でより複雑なプロポーザルを生成します。
- `docs/APP-SECURITY.md` を参照してロールと行レベルアクセスルールを追加します。
