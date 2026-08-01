mkdir -p .local/openwakeword
curl https://openwakeword.com/api/models/2276/download?format=onnx -O ./local/openwakeword/hey_findus.onnx
curl https://openwakeword.com/api/models/1131/download?format=onnx -O ./local/openwakeword/grisha.onnx
curl https://openwakeword.com/api/models/1474/download?format=onnx -O ./local/openwakeword/petrovich.onnx