/*
  Default theme script. Fields marked data-auto-submit (the collection sort
  menu, cart quantities) send their form as soon as they change.
*/
document.addEventListener('change', (event) => {
  const field = event.target.closest('[data-auto-submit]');
  if (field && field.form) field.form.requestSubmit();
});
