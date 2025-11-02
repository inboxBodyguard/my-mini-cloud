const express = require('express');
const app = express();
const port = process.env.PORT || 3000;

app.get('/', (req, res) => {
  res.json({
    message: 'Hello from Mini Cloud Platform!',
    timestamp: new Date().toISOString(),
    app: process.env.APP_NAME || 'Unknown App'
  });
});

app.listen(port, '0.0.0.0', () => {
  console.log(`App running on port ${port}`);
});