from model.transformer import Discriminator
import time
from itertools import chain
from collections import defaultdict
import pickle as pkl
import math
import numpy as np
import torch
import torch.nn.functional as F
from utils.losses import TransfoL1Loss

class TransformerTrainer():
  def __init__(self, generator, discriminator, dataloader, valid_dataloader, ce_loss, gan_loss, device, g_lr, d_lr,
               vocab_size, d_iters=5, total_iters=100000, temperature=100, gan_hp = 1, accumulation_steps=1):
    self.generator = generator
    self.discriminator = discriminator

    self.dataloader = dataloader
    self.valid_dataloader = valid_dataloader
    self.ce_loss = ce_loss
    self.gan_loss = gan_loss
    self.device=device
    
    self.g_optimizer = torch.optim.AdamW(self.generator.parameters(), lr = g_lr)#, betas=(0.9, 0.98))
    self.d_optimizer = torch.optim.AdamW(self.discriminator.parameters(), lr = d_lr)#, betas=(0.9, 0.98))

    self.vocab_size = vocab_size
    self.d_iters = d_iters
    self.temperature = temperature
    self.gan_hp = gan_hp
    self.num_iters = 0
    self.total_iters = total_iters
    self.accumulation_steps=accumulation_steps

    self.history = defaultdict(list)

    self.L1Loss = TransfoL1Loss()
   
  def train_epoch(self, log_interval=20):
    self.generator.train()
       
    losses = []
    correct_predictions = 0.0
    total_elements = 0.0
    start_time = time.time()
    index=0
  
    if isinstance(self.dataloader, list):
      # loader = chain(*self.dataloader)
      #len_loader = sum([len(l) for l in self.dataloader]) 
      loader = zip(*self.dataloader)
      len_loader = min([len(l) for l in self.dataloader] )
    else:
      loader = self.dataloader
      len_loader = len(self.dataloader)
    for data_pack in loader:
      # Solution to allow one or more dataloaders simultaneously
      data_pack = [data_pack] if not isinstance(self.dataloader, list) else data_pack
      for data in data_pack:
        input = data['input'].to(self.device)
        target = data['target'].to(self.device)
        input_mask = data['input_mask'].to(self.device)
        target_mask = data['target_mask'].to(self.device)


        if 'conditions' in data:
          conds = data['conditions'].to(self.device)
        else:
          conds = None

        if index == 0:
          b_size, seq_len = target.shape
        
        outputs,_= self.generator(input, cond=conds, input_mask=input_mask)
        #mask = torch.ones_like(inputs).bool()
        #outputs = self.generator(inputs, mask=mask)
        
        '''
        clone_out = outputs.clone()
        if (index+1)%log_interval == 0:
          unique, counts = torch.unique(torch.argmax(F.softmax(clone_out[:1], dim=1), dim = 1), sorted=True, return_counts=True)
          print(unique[torch.argsort(counts, descending=True)], len(unique))
        '''
          
        self.g_optimizer.zero_grad()                  
        loss = self.ce_loss(outputs, target, loss_mask=target_mask)
        # loss /= self.accumulation_steps
        loss.backward()
        # nn.utils.clip_grad_norm_(self.generator.parameters(), 3.0)
        self.g_optimizer.step()    
        #self.scheduler.step()

        preds = torch.argmax(F.softmax(outputs, dim=-1), dim = -1)

        correct_predictions += torch.sum((preds == target)*target_mask)
        total_elements += torch.sum(target_mask)
        #print(set(preds.reshape(-1).tolist()))
        losses.append(loss.item()*self.accumulation_steps)
                    
        if index % log_interval == 0 and index > 0:
          elapsed = time.time() - start_time
          current_loss = np.mean(losses)
          print('| {:5d} of {:5d} batches | lr {:02.7f} | ms/batch {:5.2f} | '
                'loss {:5.6f} | acc {:8.6f}'.format(
                index, len_loader, 
                2, # self.scheduler.get_last_lr()[0],
                elapsed*1000/log_interval,
                current_loss,  correct_predictions / total_elements))
          start_time = time.time()
        index+=1

    train_acc = correct_predictions/total_elements
    train_loss = np.mean(losses)
    return train_acc, train_loss, losses

  def train_epoch_gan(self, log_interval=40):
    self.generator.train()
    self.discriminator.train

    losses = []
    d_losses = []
    g_losses = []
    correct_predictions = 0.0
    total_elements = 0.0
    start_time = time.time()
    index = 0
  
    if isinstance(self.dataloader, list):
      # loader = chain(*self.dataloader)
      #len_loader = sum([len(l) for l in self.dataloader]) 
      loader = zip(*self.dataloader)
      len_loader = min([len(l) for l in self.dataloader] )
    else:
      loader = self.dataloader
      len_loader = len(self.dataloader)
    for data_pack in loader:
      # Solution to allow one or more dataloaders simultaneously
      data_pack = [data_pack] if not isinstance(self.dataloader, list) else data_pack
      for data in data_pack:
        input = data['input'].to(self.device)
                
        target = data['target'].to(self.device)
        input_mask = data['input_mask'].to(self.device)
        target_mask = data['target_mask'].to(self.device)

        if 'conditions' in data:
          conds = data['conditions'].to(self.device)
        else:
          conds = None

        if index == 0:
          b_size, seq_len = target.shape
      
        #########
        # D update
        #########      
        # Allows D to be updated
        for p in self.discriminator.parameters():
          p.requires_grad = True
        
        for _ in range(self.d_iters):
          
          d_real = self.discriminator(F.one_hot(input, num_classes=self.vocab_size), cond=conds, input_mask=input_mask)
          
          temperature = self.get_temperature()
          fake, fake_gumbel = self.generator(input, cond=conds, temperature=temperature, input_mask=input_mask)
          d_fake = self.discriminator(fake_gumbel, cond=conds, input_mask=input_mask)

          '''
          clone_out = fake.clone()
          if (index+1)%log_interval == 0:
            unique, counts = torch.unique(torch.argmax(F.softmax(clone_out[:1], dim=1), dim = 1), sorted=True, return_counts=True)
            print(unique[torch.argsort(counts, descending=True)], len(unique))
          '''

          # Chunk and calculate loss
          d_loss = self.gan_loss(self.discriminator, d_fake, fake_gumbel.data, d_real, F.one_hot(target, num_classes=self.vocab_size).data,
                                mode='d', add_disc_inputs=[conds, target_mask])
                        
          self.d_optimizer.zero_grad()                  
          # loss /= self.accumulation_steps
          d_loss.backward()
          #nn.utils.clip_grad_norm_(self.discriminator.parameters(), 3.0)
          self.d_optimizer.step()    
          #self.scheduler.step()

          d_losses.append(d_loss.item())
            
        #########
        # G update
        #########      
        # Stops D from being updated
        for p in self.discriminator.parameters():
          p.requires_grad = False      
        
        temperature=self.get_temperature()
        fake, fake_gumbel = self.generator(input, cond=conds, temperature=temperature, input_mask=input_mask)
        d_fake = self.discriminator(fake_gumbel, cond=conds, input_mask=input_mask)

        '''
        feat_loss = 0
        for feat_fake, feat_real in zip(feats_fake, feats_real):
          feat_loss += self.L1Loss(feat_fake, feat_real.detach(), self.discriminator.get_patch_loss_mask(target_mask))
        '''
        
        mle_loss = self.ce_loss(fake, target, loss_mask=target_mask)    
        gan_g_loss = self.gan_loss(self.discriminator, d_fake, mode='g')
        g_loss = mle_loss + self.gan_hp*gan_g_loss
        
        # loss /= self.accumulation_steps
        self.g_optimizer.zero_grad()    
        g_loss.backward()
        #nn.utils.clip_grad_norm_(self.generator.parameters(), 3.0)
        self.g_optimizer.step()    
        #self.scheduler.step()

        g_losses.append(gan_g_loss.item())
        losses.append(mle_loss.item()*self.accumulation_steps)

        preds = torch.argmax(F.softmax(fake, dim=-1), dim = -1)     

        correct_predictions += torch.sum((preds == target)*target_mask)
        total_elements += torch.sum(target_mask)
        #print(set(preds.reshape(-1).tolist()))
                    
        if index % log_interval == 0 and index > 0:
          elapsed = time.time() - start_time
          current_loss = np.mean(losses)
          current_d_loss = np.mean(d_losses)
          current_g_loss = np.mean(g_losses)

          print('| {:5d} of {:5d} batches | lr {:02.7f} | ms/batch {:5.2f} | '
                'loss {:5.6f} | acc {:8.6f} | D_loss: {}, G_loss: {}'.format(
                index, len_loader, 
                2, # self.scheduler.get_last_lr()[0],
                elapsed*1000/log_interval,
                current_loss,  correct_predictions/total_elements, 
                current_d_loss, current_g_loss))
          start_time = time.time()

        self.num_iters += 1
        index += 1

    train_acc = correct_predictions / total_elements
    train_loss = np.mean(losses)
    return train_acc, train_loss, losses, d_losses, g_losses
  
  def train(self, EPOCHS, checkpoint_dir, validate = False, log_interval=20, load=False, save=True, change_lr = False, train_gan=False):
    best_accuracy = 0
    total_time = 0
    valid_acc = 0
    valid_loss = 10
    
    if load:
        self.load_checkpoint(checkpoint_dir)

    if change_lr:
      new_tr_lr = 1e-5
      for param_group in self.g_optimizer.param_groups:
        param_group['lr'] = new_tr_lr
    
    for epoch in range(EPOCHS):
      epoch_start_time = time.time()
      print(f'Epoch {epoch + 1}/{EPOCHS}')

      print('-' * 10)

      d_losses, g_losses = [0], [0]
      if train_gan:
        train_acc, train_loss, train_losses, d_losses, g_losses = self.train_epoch_gan(log_interval=log_interval)
      else:
        train_acc, train_loss, train_losses = self.train_epoch(log_interval=log_interval) 
      

      self.history['train_acc'].append(train_acc)
      self.history['train_loss'].append(train_loss)
      self.history['train_losses'].append(train_losses)
      self.history['d_losses'].append(d_losses)
      self.history['g_losses'].append(g_losses)
      total_time += time.time() - epoch_start_time
      self.history['time'].append(total_time)
      if validate:
        valid_acc, valid_loss = self.evaluate(self.valid_dataloader)
        self.history['valid_acc'].append(valid_acc)
        self.history['valid_loss'].append(valid_loss)
      
      print('| End of epoch {:3d}  | time: {:5.4f}s | train loss {:5.6f} | '
            'train ppl {:8.4f} | \n train accuracy {:5.6f} | valid loss {:5.6f} | '
            'valid ppl {:8.6f} | valid accuracy {:5.6f} | D_loss: {:5.6f} | G_loss: {:5.6f} |'.format(
            epoch+1, (time.time()-epoch_start_time), train_loss, math.exp(train_loss), train_acc,
            valid_loss, math.exp(valid_loss), valid_acc, np.mean(d_losses) , np.mean(g_losses)))

      if save:
        if validate and valid_acc > best_accuracy :
          self.save_checkpoint(checkpoint_dir)
          best_accuracy = valid_acc
        elif train_acc > best_accuracy:
          self.save_checkpoint(checkpoint_dir)
          best_accuracy = train_acc

    return self.history

  def evaluate(self, eval_dataloader):
    self.generator.eval()
    eval_losses = []
    eval_correct_predictions = 0.0
    eval_total_elements = 0.0
    with torch.no_grad():
      for index, data in enumerate(eval_dataloader):
        
        input = data['input'].to(self.device)
        target = data['target'].to(self.device)
        input_mask = data['input_mask'].to(self.device)
        target_mask = data['target_mask'].to(self.device)

        if 'conditions' in data:
          conds = data['conditions'].to(self.device)
        else:
          conds = None

        if index == 0:
          b_size, seq_len = target.shape
  
        outputs, _ = self.generator(input, cond=conds, input_mask=input_mask)
        #mask = torch.ones_like(inputs).bool()
        #outputs = self.generator(inputs, mask=mask)
        
        eval_loss = self.ce_loss(outputs, target, loss_mask=target_mask)
        
        preds = torch.argmax(F.softmax(outputs, dim=-1), dim = -1)
        eval_correct_predictions += torch.sum((preds == target) * target_mask)
        eval_total_elements += torch.sum(target_mask)
        eval_losses.append(eval_loss.item())
                
    eval_acc = eval_correct_predictions / eval_total_elements
    eval_loss = np.mean(eval_losses)
        
    return eval_acc, eval_loss

  def get_temperature(self):
    temperature = self.temperature**(self.num_iters/self.total_iters)
    return temperature
  
  def save_model(self, checkpoint_dir):
    torch.save(self.model.state_dict(), checkpoint_dir + 'best_transformer_state.bin')
  
  def load_checkpoint(self, checkpoint_dir):
    checkpoint = torch.load(checkpoint_dir + 'tr_checkpoint.pth', map_location='cpu')
    self.generator.load_state_dict(checkpoint['generator'])
    self.g_optimizer.load_state_dict(checkpoint['g_optimizer'])
    self.discriminator.load_state_dict(checkpoint['discriminator'])
    self.d_optimizer.load_state_dict(checkpoint['d_optimizer'])
    with open(checkpoint_dir + 'history.pkl', 'rb') as f:
      self.history = pkl.load(f)
    self.num_iters = sum([len(tl) for tl in self.history['train_losses']])
    print(self.num_iters)
  
  def save_checkpoint(self, checkpoint_dir):
    checkpoint = { 
            'generator': self.generator.state_dict(),
            'g_optimizer': self.g_optimizer.state_dict(),
            'discriminator': self.discriminator.state_dict(),
            'd_optimizer': self.d_optimizer.state_dict()}
        
    torch.save(checkpoint, checkpoint_dir + 'tr_checkpoint.pth')
    with open(checkpoint_dir + 'history.pkl', 'wb') as f:
      pkl.dump(self.history, f)





