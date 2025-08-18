from dropbox.oauth import DropboxOAuth2FlowNoRedirect

APP_KEY = "z90ikay01suhgna"
APP_SECRET = "9cos1b2okydhacw"

flow = DropboxOAuth2FlowNoRedirect(
    APP_KEY,
    APP_SECRET,
    token_access_type="offline",
    scope=[
        "files.metadata.read",
        "files.content.read",
        "files.content.write",
        "sharing.read",
        "sharing.write",
        "account_info.read",
    ]
)

authorize_url = flow.start()
print("🔗 아래 URL로 이동해서 '허용'을 눌러주세요:")
print(authorize_url)
code = input("📋 인증 코드를 붙여넣으세요: ").strip()
token = flow.finish(code)

print("✅ Access Token:", token.access_token)
print("🔁 Refresh Token:", token.refresh_token)
