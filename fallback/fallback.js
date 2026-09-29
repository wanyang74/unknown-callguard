// Twilio Function: the phone number's "Primary handler fails" handler. Twilio runs it
// when the CallGuard server is down or errors, so the call rings the owner unscreened
// instead of being dropped. Environment variables: MY_CELL, TWILIO_NUMBER, OWNER_NAME.
exports.handler = function (context, event, callback) {
  const twiml = new Twilio.twiml.VoiceResponse();
  const voice = { voice: 'Polly.Salli-Neural' };

  // Loop guard, same as app.py: our own Dial to the cell was declined and the carrier
  // forwarded it back here. Reject so that Dial sees "busy" and goes to voicemail.
  if (event.From === context.MY_CELL || event.From === context.TWILIO_NUMBER) {
    twiml.reject({ reason: 'busy' });
    return callback(null, twiml);
  }
  // Voicemail finished (Record posts back to this URL).
  if (event.RecordingUrl) {
    twiml.say(voice, 'Thanks, your message was saved. Goodbye.');
    twiml.hangup();
    return callback(null, twiml);
  }
  // Back from the Dial below.
  if (event.DialCallStatus) {
    if (event.DialCallStatus === 'completed' || event.DialCallStatus === 'answered') {
      twiml.hangup();
    } else {
      twiml.say(voice, `Sorry, ${context.OWNER_NAME} can't pick up right now. ` +
                       'Please leave a message after the tone.');
      twiml.record({ maxLength: 120, playBeep: true });
      twiml.hangup();
    }
    return callback(null, twiml);
  }
  // Timeout 20s stays under the carrier's 30s no-answer forwarding, like RING_SECONDS.
  const dial = twiml.dial({ callerId: context.TWILIO_NUMBER, timeout: 20,
                            action: '/fallback', method: 'POST' });
  dial.number(context.MY_CELL);
  return callback(null, twiml);
};
