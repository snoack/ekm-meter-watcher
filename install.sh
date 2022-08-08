#!/bin/sh

set -eu

NAME=ekm-meter-watcher
USER=pi
CWD=$(eval echo ~$USER/$NAME)
SCRIPT=/usr/local/bin/$NAME
SCRIPT_URL=https://raw.githubusercontent.com/snoack/$NAME/main/$NAME.py
CRON_SCRIPT=/etc/cron.daily/$NAME
WATCHER_LOG=$CWD/watcher.log
CRON_LOG=$CWD/cron.log

echo "Installing dependencies..."
sudo apt-get install -y pigpio python3-pigpio sqlite3

echo "Downloading script..."
sudo curl --silent --show-error $SCRIPT_URL -o $SCRIPT
sudo chmod +x $SCRIPT

echo "Creating $CWD directory..."
sudo su $USER -c "mkdir -p $CWD"

echo "Setting up cronjob..."
sudo -s << EOS
cat << EOF > $CRON_SCRIPT
#!/bin/sh
su $USER -c 'cd $CWD && $NAME --aggregate' > $CRON_LOG 2>&1
EOF
EOS
sudo chmod +x $CRON_SCRIPT

echo "Setting up systemd service..."
sudo -s << EOS
cat << EOF > /lib/systemd/system/$NAME.service
[Unit]
Description=Script that records impulses from EKM meter via GPIO
Requires=pigpiod.service
[Service]
User=$USER
WorkingDirectory=$CWD
StandardOutput=file:$WATCHER_LOG
StandardError=file:$WATCHER_LOG
ExecStart=$SCRIPT
[Install]
WantedBy=multi-user.target
EOF
EOS
sudo systemctl enable $NAME
sudo systemctl start $NAME
sudo systemctl status $NAME

echo "$NAME was installed successfully!"
