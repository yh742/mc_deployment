#!/bin/bash

if [ ! -d "gpushare-scheduler-extender" ]
then 
   git clone https://github.com/AliyunContainerService/gpushare-scheduler-extender.git
fi

cd gpushare-scheduler-extender
docker image rm -f gpushare
docker image build . -t gpushare
docker container rm -f gpushare-build
docker run --name gpushare-build -dit gpushare /bin/bash
docker cp gpushare-build:/usr/bin/gpushare-sche-extender .
mv gpushare-sche-extender /usr/bin/


# create systemd entry
cat > /etc/systemd/system/gpushare-sche-extender.service << EOL
[Unit]
Description=Service for gpushare-sche-extender
[Service]
Environment="KUBECONFIG=$KUBECONFIG"
ExecStart=/usr/bin/gpushare-sche-extender
SyslogIdentifier=gpushare-sche-extender
Restart=on-failure
TimeoutStopSec=30
Type=simple
[Install]
WantedBy=multi-user.target
EOL
mkdir /etc/systemd/system/gpushare-sche-extender.service.d
cat > /etc/systemd/system/gpushare-sche-extender.service.d/always-restart.conf << EOL
[Unit]
StartLimitInterval=0
[Service]
RestartSec=10
EOL

systemctl start gpushare-sche-extender.service
systemctl enable gpushare-sche-extender.service

# edit original scheduler
curl -O https://raw.githubusercontent.com/AliyunContainerService/gpushare-scheduler-extender/master/config/scheduler-policy-config.json
sed -i 's/:32766/:39999/' scheduler-policy-config.json 
mv scheduler-policy-config.json /var/snap/kube-scheduler/
path=$(find /var/snap/kube-scheduler | grep -i args)
cat $path | grep -i policy-config-file
if [ ! $? -eq 0 ] 
then  
   echo --policy-config-file=/var/snap/kube-scheduler/scheduler-policy-config.json >> $path
fi
systemctl restart snap.kube-scheduler.daemon

# get device plugin 
wget -N https://raw.githubusercontent.com/AliyunContainerService/gpushare-device-plugin/master/device-plugin-rbac.yaml
wget -N https://raw.githubusercontent.com/AliyunContainerService/gpushare-device-plugin/master/device-plugin-ds.yaml
sed -i '/cpu:/d' device-plugin-ds.yaml
kubectl apply -f device-plugin-rbac.yaml
kubectl apply -f device-plugin-ds.yaml

# label nodes
for x in $(kubectl get nodes -o name);
do
   kubectl describe $x | grep -i nvidia > /dev/null
   if [ $? -eq 0 ]; then kubectl label $x gpushare=true; fi