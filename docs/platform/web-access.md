# 网页访问方式

新部署默认局域网 HTTP/IP。服务间 service_https、内部 RPC 与诊断认证维持原配置。平台新增独立网页监听（容器内 8080），只发布该端口；内部 8443 不发布到局域网。旧部署未配置 web_access 时保留旧行为，不自动降级。

`设置 → 接入准备 → 访问地址与 HTTPS` 支持三种模式：

- 直接 HTTP：访问地址如 `http://192.168.31.210:19443`。
- 直接 HTTPS：填写 HTTPS 域名或 IP，选择部署者预装证书。保存时检查证书与私钥、有效期和 SAN；不自动签发证书或创建 DNS。
- 反向代理：平台网页监听 HTTP，访问地址填写用户实际打开的外部地址，例如 `https://bot.example.test`。代理上游可以使用 NAS HTTP 地址，但必须保留外部 Host。会话 Cookie 的 Secure 属性来自登记的访问地址，不信任来客的 Forwarded/X-Forwarded-*。

每次保存需要当前登录、CSRF 与重新输入管理员密码，且使用当前配置版本防止覆盖别人修改。保存原子落盘到 `<database_path>.web-access.json`（0600），显示已保存/待重启；当前连接、来源校验与会话不会随保存改变。部署管理重启平台后才应用新设置，原进程会话失效。保存并不验证 DNS、浏览器信任或代理是否已经正确部署。无自动重启端口，无容器管理权。

部署设置示例（需已有完整 web、TLS、身份配置）：

```json
{
  "web_access": {
    "host": "0.0.0.0",
    "port": 8080,
    "initial": {
      "mode": "http",
      "origin": "http://192.168.31.210:19443",
      "certificate": null
    },
    "certificates": {
      "nas-web": {
        "certificate_file": "/etc/tianshu/tls/server.pem",
        "private_key_file": "/etc/tianshu/tls/server.key"
      }
    }
  }
}
```

`host` 与容器端口为部署者配置，不由网页更改。`initial` 只在没有已保存文件时使用；现有设置文件损坏不自动回退或显示成功。私钥只读挂载，网页只显示证书登记名称。数据目录备份必须包含访问设置文件。域名错误导致无法访问时，管理员停平台后从备份恢复该文件，或删除该文件回到 initial，再重启；不改业务数据库。

本地后端与界面测试和实际 NAS 结果分别登记在交接中。外网隧道/VPS/NPM 不在本次实施范围。
