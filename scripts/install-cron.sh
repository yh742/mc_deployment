#!/bin/bash

# find main elasticsearch node
es=$(juju status | grep elasticsearch/0 | awk '{ print $4 }')
echo $es

juju ssh $es "cat > ~/delete.sh"  <<'EOF'
#!/bin/bash

date
sudo apt list | grep -i '\<jq.*installed\>' >/dev/null
if ! [ $? -eq 0 ]
then
	sudo apt-get install -y jq
fi

json='{ 
  "aggs":{
    "uniq": {
      "terms": {
        "field": "_index"
      }
    }
  },
  "query": { 
    "range" : { 
      "@timestamp" : { 
        "lt": "now-'"$1d"'" 
      } 
    } 
  } 
}' 

indices=$(curl -XGET "http://localhost:9200/_search" -H 'Content-Type: application/json' -d "$json" 2>/dev/null | jq -r ".aggregations.uniq.buckets | .[] | .key")

# delete all relevant indices
for x in $indices
do
	echo $x
	curl -XDELETE http://localhost:9200/$x
done
EOF


juju ssh $es <<EOF 
sudo apt-get install -y jq
sudo mkdir cron
sudo chmod 777 ~/cron/
mv delete.sh ~/cron/
echo '0 2 * * * /home/ubuntu/cron/delete.sh 7 >> /home/ubuntu/cron/log.txt' | sudo crontab -
sudo chmod 777 /home/ubuntu/cron/delete.sh
EOF
