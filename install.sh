#!/bin/sh

set -eu

NAME=ekm-meter-watcher
CWD=~/$NAME
SCRIPT=$NAME.py
SCRIPT_URL=https://raw.githubusercontent.com/snoack/$NAME/main/$SCRIPT
CRON_SCRIPT=/etc/cron.daily/$NAME
WATCHER_LOG=watcher.log
CRON_LOG=cron.log

echo "Installing dependencies..."
sudo apt-get install -y pigpio python3-pigpio sqlite3

echo "Creating $CWD directory..."
mkdir -p $CWD

echo "Downloading script..."
curl --silent --show-error $SCRIPT_URL -o $CWD/$SCRIPT
chmod +x $CWD/$SCRIPT

echo "Setting up cronjob..."
crontab - << EOF
$(crontab -l 2>/dev/null | grep -v $NAME || true)
25 6	* * *	cd $CWD && ./$SCRIPT --aggregate > $CRON_LOG 2>&1
EOF

echo "Setting up systemd service..."
sudo -s << EOS
cat << EOF > /etc/systemd/system/$NAME.service
[Unit]
Description=Script that records impulses from EKM meter via GPIO
Requires=pigpiod.service
[Service]
User=$USER
WorkingDirectory=$CWD
StandardOutput=file:$CWD/$WATCHER_LOG
StandardError=file:$CWD/$WATCHER_LOG
ExecStart=$CWD/$SCRIPT
[Install]
WantedBy=multi-user.target
EOF
EOS
sudo systemctl enable $NAME
sudo systemctl start $NAME
sudo systemctl status $NAME

echo "$NAME was installed successfully!"
