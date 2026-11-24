import os
import sys
import time

import psycopg2
from werkzeug.security import generate_password_hash


def main():
    url = os.environ["WRITE_DB_URL"]
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not email or len(password) < 8:
        print("ADMIN_EMAIL бос немесе ADMIN_PASSWORD 8 таңбадан қысқа: admin жасалмады.")
        return

    last = None
    for _ in range(15):
        try:
            conn = psycopg2.connect(url, connect_timeout=4)
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO users (email, password_hash, full_name, role) "
                        "VALUES (%s, %s, 'Әкімші', 'admin') "
                        "ON CONFLICT (email) DO UPDATE SET role = 'admin'",
                        (email, generate_password_hash(password)),
                    )
            finally:
                conn.close()
            print("Admin дайын.")
            return
        except psycopg2.Error as exc:
            last = exc
            time.sleep(2)
    print(f"Admin жасау мүмкін болмады: {last}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()