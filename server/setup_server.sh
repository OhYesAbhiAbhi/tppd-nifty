set -e
cd /home/ubuntu/algo
sudo tee /etc/systemd/system/algo-web.service >/dev/null <<'EOF'
[Unit]
Description=Nifty bot phone page
After=network-online.target
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/algo
ExecStart=/home/ubuntu/algo/venv/bin/python /home/ubuntu/algo/webapp.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now algo-web >/dev/null 2>&1
sudo systemctl restart algo-web
sudo apt-get install -y -q caddy >/dev/null
printf '3-106-111-8.sslip.io {\n  reverse_proxy 127.0.0.1:8000\n}\n' | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl restart caddy
(crontab -l 2>/dev/null | grep -v reminder.py; echo '45 8 * * 1-5 cd /home/ubuntu/algo && venv/bin/python reminder.py >> cron.log 2>&1'; echo '5 9 * * 1-5 cd /home/ubuntu/algo && venv/bin/python reminder.py >> cron.log 2>&1') | crontab -
sleep 2
echo "web page: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/)  caddy: $(systemctl is-active caddy)"
echo SETUP_OK
