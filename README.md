# Smart Tourism: Nginx HTTPS және web масштабтау (Лаб 12)

Flask қосымшасы + PostgreSQL primary/standby репликациясы. Алдына Nginx reverse proxy
қойылған: HTTPS, round robin жүктеме теңгеру, static файлдар, health тексерулері.

## Архитектура

Браузер → HTTPS Nginx → web / web_replica → PostgreSQL primary ⇒ standby

| Сервис | Міндеті | Сыртқы порт |
|---|---|---|
| nginx | TLS, reverse proxy, static, жүктеме бөлу | 80, 443 |
| web, web_replica | Қосымшаның екі бірдей данасы | жоқ (expose 5000) |
| postgres_primary | Барлық жазу операциялары | жоқ |
| postgres_standby | Реплика, тек оқу | жоқ |

## 1. Талаптар

Docker Desktop (немесе Docker Engine + Compose), Git, OpenSSL (Git Bash/WSL ішінде бар).

## 2. `.env` файлын жасау

Репозиторийде `.env` жоқ (құпия). Жоба түбірінде өзіңіз жасаңыз:

```
SECRET_KEY=ұзын_кездейсоқ_жол
GITHUB_URL=https://github.com/<user>/<repo>
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=күшті_пароль
```

## 3. Сертификат жасау

Private key репозиторийге жүктелмейді (`.gitignore`). Өз компьютеріңізде жасаңыз
(Git Bash немесе WSL):

```bash
mkdir -p nginx/certs
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout nginx/certs/lab.local.key \
  -out nginx/certs/lab.local.crt \
  -subj "/CN=lab.local" \
  -addext "subjectAltName=DNS:lab.local"

openssl x509 -in nginx/certs/lab.local.crt -noout -subject -dates -ext subjectAltName
```

SAN өрісінде `DNS:lab.local` көрінуі керек.

## 4. hosts жазбасы

Windows (PowerShell, **Administrator**):

```powershell
Add-Content C:\Windows\System32\drivers\etc\hosts "127.0.0.1  lab.local"
```

Linux/WSL:

```bash
echo "127.0.0.1  lab.local" | sudo tee -a /etc/hosts
```

## 5. Іске қосу

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose exec nginx nginx -t
```

Бес сервис те `healthy` болуы тиіс. Дерекқор томдарын өшірмеңіз (`down -v` қолданбаңыз).

## 6. Тексеру

```bash
curl -I http://lab.local              # 301 -> https://lab.local/
curl -k https://lab.local/nginx-health # nginx ok
curl -kI https://lab.local/static/style.css  # 200, Server: nginx, Cache-Control
for i in 1 2 3 4 5 6; do curl -sk https://lab.local/health; echo; done
```

`/health` жауабындағы `instance` екі түрлі болуы керек (балансировка).
Браузерде: https://lab.local (self-signed сертификат ескертуі: Advanced → Proceed).

Репликацияны тексеру:

```bash
docker compose exec postgres_primary psql -U appuser -d projectdb \
  -c "SELECT state, sync_state FROM pg_stat_replication;"
```

## 7. Failover сынағы

```bash
docker compose stop web
docker compose ps
curl -k https://lab.local/health        # instance тек web_replica
docker compose logs --tail=50 nginx

docker compose start web
docker compose ps                       # web қайтадан healthy
```

## 8. Қауіпсіздік

- `.env` және `nginx/certs/*.key` репозиторийде жоқ.
- Тек TLS 1.2 және 1.3 рұқсат етілген.
- web және PostgreSQL порттары host интерфейсіне жарияланбаған.
- `X-Forwarded-For` және `X-Forwarded-Proto` қосымшаға беріледі (Flask-та `ProxyFix`).
- Өндірістік ортада self-signed сертификаттың орнына сенімді CA сертификаты қолданылады.