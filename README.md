# Sharpener-Gyro

```bash
mpremote connect /dev/ttyUSB0 cp main.py :main.py
mpremote connect /dev/ttyUSB0 cp reader.py :reader.py
mpremote connect /dev/ttyUSB0 cp index.html :index.html
mpremote connect /dev/ttyUSB0 cp settings.html :settings.html
mpremote connect /dev/ttyUSB0 exec "import machine; machine.reset()"
```