
#!/bin/bash

#sleep 1.5

####################
# play startup sound
echo "Playing startup sound"
amixer sset 'PCM' 80%
aplay /home/pi/Music/startupsound.wav -D softvol_and_pivumeter
echo "Startup sound played"

#mpgvolume=$((8192))
#echo "${mpgvolume} is the mpg123 startup volume"
#/usr/bin/mpg123 -f -${mpgvolume} /home/pi/RPi-Jukebox-RFID/shared/startupsound.mp3

#######################
