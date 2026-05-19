# Faraway BackEnd

Faraway 单仓库中的后端项目。

当前技术栈：

- FastAPI
- SQLite
- 阿里云 OSS
- 阿里云百炼
- JWT 鉴权

## 当前认证方案

现在已经改成真实用户隔离：

- 用户先注册
- 再登录
- 后端为每个用户签发独立 token
- 用户资料保存在 SQLite
- 业务数据按 `current_user.uid` 隔离

默认配置下：

- `DEV_ALLOW_ANON_AUTH=false`
- `DEV_TRUST_X_USER_HEADERS=false`

也就是说，不再默认允许匿名访问，也不再默认信任 `X-User-Id` 伪造用户。

## 目录

```text
BackEnd/
├─ app/
│  ├─ core/
│  ├─ models/
│  ├─ routers/
│  └─ services/
├─ data/
├─ .env
├─ .env.example
└─ requirements.txt
```

配套文档已统一放到仓库根目录的 `docs/` 下。

## 启动

```bash
cd BackEnd
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

启动后访问：

- `http://127.0.0.1:8000/docs`
- `http://127.0.0.1:8000/health`

## 配置

直接编辑 `.env`：

- `SECRET_KEY`
- `DASHSCOPE_API_KEY`
- `WECHAT_APP_ID`
- `WECHAT_APP_SECRET`
- `OSS_ACCESS_KEY_ID`
- `OSS_ACCESS_KEY_SECRET`
- `OSS_ENDPOINT`
- `OSS_BUCKET_NAME`
- `OSS_PUBLIC_BASE_URL`

认证相关配置：

- `DEV_ALLOW_ANON_AUTH=false`
- `DEV_TRUST_X_USER_HEADERS=false`
- `PASSWORD_HASH_ITERATIONS=120000`
- `SMS_CODE_TTL_MINUTES=10`

## 已实现认证接口

- `POST /api/auth/register`
- `POST /api/auth/password-login`
- `POST /api/auth/send-sms-code`
- `POST /api/auth/phone-login`
- `POST /api/auth/wechat`
- `POST /api/auth/wechat-login`
- `POST /api/auth/logout`

说明：

- `demo-login` 只在 `DEV_ALLOW_ANON_AUTH=true` 时可用
- 手机登录要求手机号已注册并且短信验证码有效
- 微信登录首次成功后会自动落库成用户账号

## 返回格式

所有业务接口统一返回：

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```
